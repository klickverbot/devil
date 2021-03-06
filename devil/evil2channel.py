from PyQt4 import QtCore as QtC
from PyQt4 import QtGui as QtG
from PyQt4 import uic
from devil.channel import Channel, ErrorCondition, Register
from devil.controlpanel import ControlPanel


PID_RST_N = 1 << 0
RAMP_RST_N = 1 << 1
OUTPUT_SEL = 1 << 2
PID_POLARITY = 1 << 3
LD_ON = 1 << 4
SWEEPING_MASK = PID_RST_N | RAMP_RST_N | OUTPUT_SEL
SWEEPING_STATE = RAMP_RST_N | OUTPUT_SEL

HW_CLOCK_INTERVAL_SECS = 1 / 96e6
HW_RAMP_CNT_WIDTH = 44
HW_RAMP_OUTPUT_WIDTH = 16

STREAM_NAMES = [
    'ADC (error signal)',
    'PID/ramp output',
    'Relocking slow lowpass filter',
    'Relocking filter difference'
]


def _decode_status(control_reg_val, sweep_range_reg_val):
    if (control_reg_val & SWEEPING_MASK) == SWEEPING_STATE:
        if sweep_range_reg_val == 0:
            return Channel.Status.idle
        else:
            return Channel.Status.configuring
    return Channel.Status.running


def _sweep_timings(freq_reg_val):
    counter_steps = ((1 << (HW_RAMP_CNT_WIDTH - HW_RAMP_OUTPUT_WIDTH)) - 1)
    up_secs = counter_steps / freq_reg_val * HW_CLOCK_INTERVAL_SECS
    down_secs = counter_steps // 8 / freq_reg_val * HW_CLOCK_INTERVAL_SECS
    return up_secs, down_secs


class Evil2Channel(Channel):
    def __init__(self, zmq_ctx, host_addr, resource):
        Channel.__init__(self, zmq_ctx, host_addr, resource)

        self._system_control_reg = Register(0)

        self._system_condition_reg = Register(30)
        self._system_condition_reg.changed.connect(self._condition_reg_changed)
        self._cond_mask_to_error = {
            0b1: ErrorCondition('ADC_RANGE', 'Analog input out of range')
        }

        self._widget_name_to_reg = {
            'centerSpinBox': Register(1, True),
            'rangeSpinBox': Register(2),
            'frequencySpinBox': Register(3),
            'inputOffsetSpinBox': Register(4, True),
            'outputOffsetSpinBox': Register(5, True),
            'pGainSpinBox': Register(6),
            'iGainSpinBox': Register(7),
            'dGainSpinBox': Register(8),
            'filterResponseSpinBox': Register(9, True),
            'thresholdSpinBox': Register(10),
            'ttlExpSpinBox': Register(11)
        }

        self._current_status = Channel.Status.idle
        self._system_control_reg.changed.connect(self._update_status)
        self._widget_name_to_reg['rangeSpinBox'].changed.connect(
            self._update_status)
        self._update_status()

    def unlock(self):
        self._widget_name_to_reg['rangeSpinBox'].set_from_local_change(0)
        self._system_control_reg.set_from_local_change(
            (self._system_control_reg.sval & ~SWEEPING_MASK) | SWEEPING_STATE)

    def current_error_conditions(self):
        return [e for m, e in self._cond_mask_to_error.items()
                if self._system_condition_reg.sval & m]

    def current_status(self):
        return self._current_status

    def registers(self):
        regs = list(self._widget_name_to_reg.values())
        regs.append(self._system_control_reg)
        regs.append(self._system_condition_reg)
        return regs

    def _condition_reg_changed(self):
        self.error_conditions_changed.emit(self.current_error_conditions())

    def _update_status(self):
        new_status = _decode_status(self._system_control_reg.sval,
                                    self._widget_name_to_reg[
                                        'rangeSpinBox'].sval)
        if self._current_status == new_status:
            return
        self._current_status = new_status
        self.status_changed.emit(new_status)


def create_evil2_control_panel(version_string, channel):
    reg_area = Evil2RegisterArea(channel._system_control_reg,
                                 channel._widget_name_to_reg)

    cp = ControlPanel(version_string, channel.resource.display_name,
                      STREAM_NAMES, reg_area)

    cp.set_error_conditions(channel.current_error_conditions())
    channel.error_conditions_changed.connect(cp.set_error_conditions)

    return cp


class Evil2RegisterArea(QtG.QWidget):
    extra_plot_items_changed = QtC.pyqtSignal(dict)

    def __init__(self, system_control_reg, register_name_map):
        QtG.QWidget.__init__(self)

        uic.loadUi('ui/evil2registerarea.ui', self)

        self._system_control_reg = system_control_reg
        self._system_control_reg.changed.connect(self._set_control_flags)
        self._set_control_flags(system_control_reg.sval)

        self._widgets_to_save = []
        for widget_name, register in register_name_map.items():
            self._widgets_to_save.append(widget_name)
            widget = getattr(self, widget_name)
            register.changed.connect(widget.setValue)
            widget.setValue(register.sval)
            widget.valueChanged.connect(register.set_from_local_change)

        self.sweepButton.clicked.connect(self.toggle_sweep)
        self.flipPolarityButton.clicked.connect(self.toggle_polarity)
        self.resetPidButton.clicked.connect(self.pid_reset)
        self.relockingEnabledCheckBox.clicked.connect(self.toggle_relocking)

        self._system_control_reg.changed.connect(
            self._emit_extra_plot_items_changed)
        self.frequencySpinBox.valueChanged.connect(
            self._emit_extra_plot_items_changed)
        self.inputOffsetSpinBox.valueChanged.connect(
            self._emit_extra_plot_items_changed)
        self.rangeSpinBox.valueChanged.connect(
            self._emit_extra_plot_items_changed)
        self.thresholdSpinBox.valueChanged.connect(
            self._emit_extra_plot_items_changed)

    def load_settings(self, settings):
        for key, value in settings.items():
            if key == 'systemControl':
                self._system_control_reg.set_from_local_change(value)
                continue

            widget = getattr(self, key, None)
            if widget:
                widget.setValue(value)

    def save_settings(self):
        settings = {}
        for key in self._widgets_to_save:
            settings[key] = getattr(self, key).value()
        settings['systemControl'] = self._control_flags
        return settings

    def extra_plot_items(self):
        value = {0: {'offset': self.inputOffsetSpinBox.value()},
                 3: {'threshold': self.thresholdSpinBox.value()}}

        status = _decode_status(self._system_control_reg.sval,
                                self.rangeSpinBox.value())
        if status == Channel.Status.configuring:
            up_time, down_time = _sweep_timings(self.frequencySpinBox.value())
            # Show lines for the sweep period, and for when the sweep center is
            # reached during the up sweep as well as when the down sweep starts.
            ticks = (up_time + down_time, [up_time / 2, up_time])
            for i in range(4):
                if i not in value:
                    value[i] = {}
                value[i]['period'] = ticks

        return value

    def _set_control_flags(self, flags):
        self._control_flags = flags
        self.relockingEnabledCheckBox.setChecked(flags & LD_ON)

        if flags & PID_POLARITY:
            self.flipPolarityButton.setStyleSheet('QPushButton {color: blue}')
        else:
            self.flipPolarityButton.setStyleSheet('QPushButton {color: green}')

        if (flags & SWEEPING_MASK) == SWEEPING_STATE:
            self.sweepButton.setText('Sweeping')
            self.sweepButton.setStyleSheet('QPushButton {color: green}')
        else:
            self.sweepButton.setText('Controlling')
            self.sweepButton.setStyleSheet('QPushButton {color: blue}')

    def _emit_extra_plot_items_changed(self):
        self.extra_plot_items_changed.emit(self.extra_plot_items())

    def pid_reset(self):
        self.pid_off()
        self.pid_on()

    def toggle_relocking(self):
        self._control_flags ^= LD_ON
        self._system_control_reg.set_from_local_change(self._control_flags)

    def toggle_polarity(self):
        pid_was_on = self.pid_off()

        self._control_flags ^= PID_POLARITY
        self._system_control_reg.set_from_local_change(self._control_flags)

        if pid_was_on:
            self.pid_on()

    def toggle_sweep(self):
        # Only read one bit here but write all to be resilient against invalid
        # states (e.g. when somebody loads an old parameter save file).
        sweeping = self._control_flags & OUTPUT_SEL
        self._control_flags &= ~SWEEPING_MASK

        if sweeping:
            self._control_flags |= ((~SWEEPING_STATE) & SWEEPING_MASK)
        else:
            self._control_flags |= (SWEEPING_STATE & SWEEPING_MASK)

        self._system_control_reg.set_from_local_change(self._control_flags)

    def pid_off(self):
        if not (self._control_flags & PID_RST_N):
            return False

        self._control_flags &= ~PID_RST_N
        self._system_control_reg.set_from_local_change(self._control_flags)
        return True

    def pid_on(self):
        self._control_flags |= PID_RST_N
        self._system_control_reg.set_from_local_change(self._control_flags)
