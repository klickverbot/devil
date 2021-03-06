import msgpack
import numpy as np
import qtzmq
import zmq

from enum import Enum, unique
from PyQt4 import QtCore as QtC

MSGPACKRPC_REQUEST = 0
MSGPACKRPC_RESPONSE = 1
MSGPACKRPC_NOTIFICATION = 2

MSGPACK_EXT_INT8ARRAY = 1

HEARTBEAT_INTERVAL_MSECS = 5000
HEARTBEAT_TIMEOUT_MSECS = 2000


class Register(QtC.QObject):
    changed_locally = QtC.pyqtSignal(int, int)

    # Catch-all for both local and remote changes.
    changed = QtC.pyqtSignal(int)

    desynchronized = QtC.pyqtSignal()

    def __init__(self, idx, is_signed=False):
        QtC.QObject.__init__(self)

        self.idx = idx
        self._is_signed = is_signed
        self._remote_updates_to_ignore = []
        self._uval = 0
        self._synchronized = False

    @property
    def sval(self):
        return self._from_unsigned(self._uval)

    def set_from_local_change(self, new_sval):
        if not self._synchronized:
            return

        new_uval = self._to_unsigned(new_sval)
        if self._uval == new_uval:
            return

        old_uval = self._uval
        self._uval = new_uval
        self._remote_updates_to_ignore.append(new_uval)
        self.changed_locally.emit(old_uval, new_uval)
        self.changed.emit(self.sval)

    def set_from_remote_notification(self, new_uval):
        if not self._synchronized:
            return

        try:
            i = self._remote_updates_to_ignore.index(new_uval)
            self._remote_updates_to_ignore = self._remote_updates_to_ignore[
                                             i + 1:]
        except ValueError:
            self._remote_updates_to_ignore.clear()
            if new_uval != self._uval:
                self._uval = new_uval
                self.changed.emit(self.sval)

    def set_from_remote_query(self, new_uval):
        self._uval = new_uval
        self._synchronized = True
        self.changed.emit(self.sval)

    def mark_as_desynchronized(self):
        self._synchronized = False
        self.desynchronized.emit()

    def _from_unsigned(self, uval):
        if self._is_signed and uval >= 2 ** 15:
            return uval - 2 ** 16
        return uval

    def _to_unsigned(self, sval):
        if self._is_signed and sval < 0:
            return sval + 2 ** 16
        return sval


class ErrorCondition:
    def __init__(self, short_name, long_name):
        self.short_name = short_name
        self.long_name = long_name


class StreamPacket:
    def __init__(self, stream_idx, sample_interval_seconds, trigger_offset,
                 data_type, data_buffer):
        self.stream_idx = stream_idx
        self.sample_interval_seconds = sample_interval_seconds
        self.trigger_offset = trigger_offset

        if data_type == MSGPACK_EXT_INT8ARRAY:
            self.samples = np.fromstring(data_buffer, np.int8).astype(np.int16)

            # To match true 10 bit range of hardware resolution; mainly to
            # account for an eventual upgrade and to not break user expectations
            # from the old client.
            self.samples *= 4
        else:
            raise Exception(
                'Unknown stream sample data type: {}'.format(data_type))


class Channel(QtC.QObject):
    @unique
    class Status(Enum):
        idle = 0
        configuring = 1
        running = 2

    connection_ready = QtC.pyqtSignal()
    connection_failed = QtC.pyqtSignal(str)
    shutting_down = QtC.pyqtSignal()
    error_conditions_changed = QtC.pyqtSignal(list)
    status_changed = QtC.pyqtSignal(Status)
    stream_packet_received = QtC.pyqtSignal(StreamPacket)
    stream_acquisition_config_changed = QtC.pyqtSignal(float, int)

    def __init__(self, zmq_ctx, host_addr, resource):
        QtC.QObject.__init__(self)

        self.resource = resource

        self._zmq_ctx = zmq_ctx
        self._host_addr = host_addr
        self._pending_rpc_request = None
        self._rpc_request_queue = []
        self._active_stream_sockets = {}

        self._reg_idx_to_object = {}

        self._rpc_socket = qtzmq.Socket(zmq_ctx, zmq.REQ)
        self._rpc_socket.received_msg.connect(
            lambda m: QtC.qCritical(
                'Unhandled message on RPC socket: {}'.format(m)))
        self._rpc_socket.error.connect(self._socket_error)
        self._rpc_socket.connect(self._remote_endpoint(resource.port))

        self._heartbeat_send_timer = QtC.QTimer()
        self._heartbeat_send_timer.timeout.connect(self._send_heartbeat)

        self._heartbeat_timeout_timer = QtC.QTimer()
        self._heartbeat_timeout_timer.timeout.connect(self._heartbeat_timed_out)

        self._stream_ports = []
        self._stream_subscriber_count = {}
        self._stream_acquisition_config = None

        self._invoke_rpc('notificationPort', [], self._got_notification_port)

    def unlock(self):
        raise NotImplementedError('Need to implement function that unlocks '
                                  'the controller for this specific channel '
                                  'type.')

    def current_error_conditions(self):
        raise NotImplementedError('Need to implement error condition reading '
                                  'for this specific channel type.')

    def current_status(self):
        raise NotImplementedError('Need to implement status reading for this '
                                  'specific channel type.')

    def add_stream_subscription(self, stream_idx):
        old_count = self._stream_subscriber_count.get(stream_idx, 0)
        self._stream_subscriber_count[stream_idx] = old_count + 1

        if old_count == 0:
            s = qtzmq.Socket(self._zmq_ctx, zmq.SUB)
            s.received_msg.connect(
                lambda msg: self._got_stream_packet(stream_idx, msg))
            s.connect(self._remote_endpoint(self._stream_ports[stream_idx]))
            self._active_stream_sockets[stream_idx] = s

    def remove_stream_subscription(self, stream_idx):
        self._stream_subscriber_count[stream_idx] -= 1

        if self._stream_subscriber_count[stream_idx] == 0:
            # Check whether we are still connected to make channel shutdown code
            # less order-sensitive.
            if stream_idx in self._active_stream_sockets:
                self._active_stream_sockets[stream_idx].close()
                del self._active_stream_sockets[stream_idx]

    def stream_acquisition_config(self):
        return self._stream_acquisition_config

    def set_stream_acquisition_config(self, time_span_seconds, points):
        config = time_span_seconds, points
        if config != self._stream_acquisition_config:
            self._invoke_rpc('setStreamAcquisitionConfig', config)

    def _modify_register(self, reg_idx, old_val, new_val):
        self._invoke_rpc('modifyRegister', [reg_idx, old_val, new_val],
                         lambda succeeded: succeeded or self._register_conflict(
                             reg_idx))

    def registers(self):
        return []

    def _register_conflict(self, reg_idx):
        self._reg_idx_to_object[reg_idx].mark_as_desynchronized()
        self._read_registers([reg_idx])

    def _got_notification_port(self, port):
        self._notification_socket = qtzmq.Socket(self._zmq_ctx, zmq.SUB)
        self._notification_socket.error.connect(self._socket_error)
        self._notification_socket.connect(self._remote_endpoint(port))
        self._notification_socket.received_msg.connect(self._got_notification)

        self._invoke_rpc('streamPorts', [], self._got_stream_ports)

    def _got_stream_ports(self, ports):
        self._stream_ports = ports

        # Initialize registers.
        regs = self.registers()
        for r in regs:
            self._reg_idx_to_object[r.idx] = r
            r.changed_locally.connect(lambda old_val, new_val, idx=r.idx:
                                      self._modify_register(idx, old_val,
                                                            new_val))

        self._read_registers([r.idx for r in regs],
                             self._read_stream_acquisition_config)

    def _read_registers(self, registers, completion_handler=None):
        if not registers:
            if completion_handler:
                completion_handler()
            return

        idx = registers.pop(0)

        def handle(val):
            self._reg_idx_to_object[idx].set_from_remote_query(val)
            self._read_registers(registers, completion_handler)

        self._invoke_rpc('readRegister', [idx], handle)

    def _read_stream_acquisition_config(self):
        self._invoke_rpc('streamAcquisitionConfig', [],
                         self._got_stream_acquisition_config)

    def _got_stream_acquisition_config(self, val):
        time_span_seconds, points = val
        self._stream_acquisition_config = (time_span_seconds, points)

        self._heartbeat_send_timer.start(HEARTBEAT_INTERVAL_MSECS)
        self.connection_ready.emit()

    def _shutdown(self):
        self._rpc_socket.close()
        self._heartbeat_send_timer.stop()
        self._heartbeat_timeout_timer.stop()
        self._notification_socket.close()
        for s in self._active_stream_sockets.values():
            s.close()
        self._active_stream_sockets.clear()

        self.shutting_down.emit()

    def _got_notification(self, msg):
        try:
            msg_type, method, params = msgpack.unpackb(msg, encoding='utf-8')
            if msg_type != MSGPACKRPC_NOTIFICATION:
                self._rpc_error(
                    'Expected msgpack-rpc notification, but got: {}'.format(
                        type))
                return

            if method == 'registerChanged':
                idx, value = params
                self._reg_idx_to_object[idx].set_from_remote_notification(value)
                return

            if method == 'streamAcquisitionConfigChanged':
                time_span_seconds, points = params
                self._stream_acquisition_config = (time_span_seconds, points)
                self.stream_acquisition_config_changed.emit(
                    *self._stream_acquisition_config)
                return

            if method == 'shutdown':
                self._shutdown()
                return

            QtC.qWarning(
                'Received unknown notification type: {}{}'.format(method,
                                                                  params))
        except Exception as e:
            self._rpc_error('Error while handling notification: {} ({})'.format(e, type(e)))

    def _got_stream_packet(self, stream_idx, msg):
        try:
            msg_type, method, params = msgpack.unpackb(msg, encoding='utf-8')
            if msg_type != MSGPACKRPC_NOTIFICATION:
                self._rpc_error('Expected msgpack-rpc notification for '
                                'streaming packet, but got: {}'.format(msg_type))
                return

            if method != 'streamPacket':
                self._rpc_error(
                    'Invalid method on streaming socket: {}'.format(method))
                return

            p = params[0]
            interval = p['sampleIntervalSeconds']
            trigger = p['triggerOffset']
            sample_type = p['samples'].code
            sample_buffer = p['samples'].data
            packet = StreamPacket(stream_idx, interval, trigger, sample_type,
                                  sample_buffer)

            self.stream_packet_received.emit(packet)
        except Exception as e:
            self._rpc_error('Error while handling stream packet: {}'.format(e))

    def _invoke_rpc(self, method, args, response_handler=None):
        # We do not use the sequence id, just pass zero.
        request = msgpack.packb((MSGPACKRPC_REQUEST, 0, method, args))

        self._rpc_request_queue.append((request, response_handler))
        if not self._pending_rpc_request:
            self._send_next_rpc_request()

    def _got_rpc_response(self, response):
        try:
            msg_type, seq_id, err, ret_val = msgpack.unpackb(response,
                                                             encoding='utf-8')
            if msg_type != MSGPACKRPC_RESPONSE:
                self._rpc_error(
                    'Unexpected msgpack-rpc message type: {}'.format(msg_type))
            elif err:
                self._rpc_error(err)
            else:
                _, response_handler = self._pending_rpc_request
                if response_handler:
                    response_handler(ret_val)

            self._pending_rpc_request = None
            if self._rpc_request_queue:
                self._send_next_rpc_request()
        except Exception as e:
            self._rpc_error(e)

    def _send_next_rpc_request(self):
        self._pending_rpc_request = self._rpc_request_queue.pop(0)
        self._rpc_socket.request(self._pending_rpc_request[0],
                                 self._got_rpc_response)

    def _send_heartbeat(self):
        if self._heartbeat_timeout_timer.isActive():
            # Still waiting for a previous heartbeat reply.
            return

        self._invoke_rpc('ping', [], self._got_heartbeat)
        self._heartbeat_timeout_timer.start(HEARTBEAT_TIMEOUT_MSECS)

    def _got_heartbeat(self, _):
        self._heartbeat_timeout_timer.stop()

    def _heartbeat_timed_out(self):
        self._heartbeat_timeout_timer.stop()
        self.connection_failed.emit('Connection timed out.')
        self._shutdown()

    def _socket_error(self, err):
        self.connection_failed.emit('Socket error: {}'.format(err))
        self._shutdown()

    def _rpc_error(self, err):
        self.connection_failed.emit('RPC error: {}'.format(err))
        self._shutdown()

    def _remote_endpoint(self, port):
        return 'tcp://{}:{}'.format(self._host_addr.toString(), port)
