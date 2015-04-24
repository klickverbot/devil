from PyQt5 import QtCore as QtC
from PyQt5 import QtWidgets as QtW
from PyQt5.uic import loadUi


HEADER_SETTING = 'device_list_header'
IN_DASHBOARD_SETTINGS = 'show_in_dashboard/'


class DeviceList(QtW.QWidget):
    def __init__(self):
        QtW.QWidget.__init__(self)
        loadUi('ui/devicelist.ui', self)

        s = QtC.QSettings()
        if s.contains(HEADER_SETTING):
            self.deviceTableWidget.horizontalHeader().restoreState(
                s.value(HEADER_SETTING))

        self._channels = []

    def register(self, channel):
        # We need to keep a reference to the object around as connecting to a
        # signal only creates a weak references.
        self._channels.append(channel)
        channel.connection_ready.connect(lambda: self._display_channel(channel))
        channel.shutting_down.connect(lambda: self._remove_channel(channel))

    def closeEvent(self, event):
        state = self.deviceTableWidget.horizontalHeader().saveState()
        QtC.QSettings().setValue(HEADER_SETTING, state)

    def _display_channel(self, channel):
        tw = self.deviceTableWidget
        row = tw.rowCount()
        tw.insertRow(row)

        tw.setItem(row, 0, QtW.QTableWidgetItem(channel.resource.display_name))

        dev_id = channel.resource.dev_id
        tw.setItem(row, 1, QtW.QTableWidgetItem(dev_id))

        tw.setItem(row, 2, QtW.QTableWidgetItem(str(channel.resource.version)))

        show_in_dashboard = QtW.QCheckBox()
        show_in_dashboard.setChecked(self._load_dashboard_state(dev_id))
        show_in_dashboard.stateChanged.connect(
            lambda val: self._save_dashboard_state(dev_id, val))
        tw.setCellWidget(row, 3, show_in_dashboard)

        open_button = QtW.QPushButton('Control Panel')
        open_button.clicked.connect(channel.show_control_panel)
        tw.setCellWidget(row, 4, open_button)

    def _remove_channel(self, channel):
        idx = self._channels.index(channel)
        self.deviceTableWidget.removeRow(idx)
        self._channels.remove(channel)

    def _load_dashboard_state(self, dev_id):
        s = QtC.QSettings()
        return int(s.value(IN_DASHBOARD_SETTINGS + dev_id, 2))

    def _save_dashboard_state(self, dev_id, val):
        s = QtC.QSettings()
        s.setValue(IN_DASHBOARD_SETTINGS + dev_id, val)