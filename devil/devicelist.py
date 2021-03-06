from PyQt4 import QtCore as QtC
from PyQt4 import QtGui as QtG
from PyQt4.uic import loadUi
from devil.dashboard import Dashboard
from devil.guichannel import GuiChannel

HEADER_SETTING = 'device_list_header'
IN_DASHBOARD_SETTINGS = 'show_in_dashboard/'


class DeviceList(QtG.QWidget):
    closed = QtC.pyqtSignal()
    force_rescan = QtC.pyqtSignal()

    def __init__(self, version_string):
        QtG.QWidget.__init__(self)
        loadUi('ui/devicelist.ui', self)

        self._version_string = version_string
        self.setWindowTitle('Device List – DEVIL ' + version_string)

        s = QtC.QSettings()
        if s.contains(HEADER_SETTING):
            self.deviceTableWidget.horizontalHeader().restoreState(
                s.value(HEADER_SETTING))

        self.forceRescanButton.clicked.connect(self.force_rescan)
        self.openDashboardButton.clicked.connect(self._open_dashboard)

        self.guichannels = []
        self.guichannels_displayed = []

        self._channel_in_dashboard_boxes = {}

        self._dashboard = None

    def register(self, channel, create_control_panel_fn):
        # We need to keep a reference to the object around as connecting to a
        # signal only creates a weak references.
        guichannel = GuiChannel(channel, create_control_panel_fn)
        self.guichannels.append(guichannel)

        channel.connection_ready.connect(lambda: self._display_channel(guichannel))
        channel.shutting_down.connect(lambda: self._remove_channel(guichannel))
        channel.connection_failed.connect(self._channel_connection_failed)

    def _channel_connection_failed(self, msg):
        QtC.qWarning('[{}] Connection failed: {}'.format(
            self.sender().resource.display_name, msg))

        # Immediately rescan to swiftly recover from intermittent connection
        # problems.
        self.force_rescan.emit()

    def closeEvent(self, event):
        state = self.deviceTableWidget.horizontalHeader().saveState()
        QtC.QSettings().setValue(HEADER_SETTING, state)

        self.closed.emit()

    def _display_channel(self, guichannel):
        self.guichannels_displayed.append(guichannel)
        self.guichannels_displayed.sort(
            key=lambda a: a.channel.resource.display_name)

        tw = self.deviceTableWidget
        row = self.guichannels_displayed.index(guichannel)
        tw.insertRow(row)

        c = guichannel.channel
        tw.setItem(row, 0, QtG.QTableWidgetItem(c.resource.display_name))

        dev_id = c.resource.dev_id
        tw.setItem(row, 1, QtG.QTableWidgetItem(dev_id))

        tw.setItem(row, 2, QtG.QTableWidgetItem(str(c.resource.version)))

        show_in_dashboard = QtG.QCheckBox()
        on_dashboard = self._load_show_in_dashboard(dev_id)
        show_in_dashboard.setChecked(on_dashboard)
        show_in_dashboard.stateChanged.connect(
            lambda val: self._show_in_dashboard_changed(guichannel, val))
        self._channel_in_dashboard_boxes[guichannel] = show_in_dashboard
        tw.setCellWidget(row, 3, show_in_dashboard)

        open_button = QtG.QPushButton('Control Panel')
        open_button.clicked.connect(guichannel.show_control_panel)
        tw.setCellWidget(row, 4, open_button)

        # If the channel had been on the dashboard before and the dashboard is
        # open, immediately show it.
        if on_dashboard and self._dashboard:
            self._dashboard.add_channel(guichannel)

    def _remove_channel(self, guichannel):
        if guichannel in self.guichannels_displayed:
            idx = self.guichannels_displayed.index(guichannel)
            self.deviceTableWidget.removeRow(idx)
            del self._channel_in_dashboard_boxes[guichannel]
            self.guichannels_displayed.remove(guichannel)
        self.guichannels.remove(guichannel)

    def _show_in_dashboard_changed(self, guichannel, new_val):
        s = QtC.QSettings()
        s.setValue(IN_DASHBOARD_SETTINGS + guichannel.channel.resource.dev_id,
            new_val)

        if self._dashboard:
            if new_val == 2:
                self._dashboard.add_channel(guichannel)
            elif new_val == 0:
                self._dashboard.remove_channel(guichannel)

    def _load_show_in_dashboard(self, dev_id):
        s = QtC.QSettings()
        return int(s.value(IN_DASHBOARD_SETTINGS + dev_id, 0))

    def _open_dashboard(self):
        if not self._dashboard:
            self._dashboard = Dashboard(self._version_string)
            to_show = [c for c in self.guichannels
                       if self._load_show_in_dashboard(c.channel.resource.dev_id)]
            self._dashboard.add_channels(to_show)
            self._dashboard.closed.connect(self._dashboard_closed)
            self._dashboard.hide_channel.connect(self._hide_from_dashboard)
            self._dashboard.show()

    def _hide_from_dashboard(self, channel):
        checkbox = self._channel_in_dashboard_boxes.get(channel, None)
        if checkbox:
            checkbox.setChecked(0)

    def _dashboard_closed(self):
        self._dashboard = None