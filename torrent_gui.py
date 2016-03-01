#!/usr/bin/python3
# -*- coding: utf-8 -*-
import argparse
import asyncio
import logging
import os
import sys
from contextlib import closing
from functools import partial
from math import floor
from typing import Dict

# noinspection PyUnresolvedReferences
from PyQt5.QtCore import Qt, QThread
# noinspection PyUnresolvedReferences
from PyQt5.QtGui import QIcon, QFont
# noinspection PyUnresolvedReferences
from PyQt5.QtWidgets import QWidget, QListWidget, QAbstractItemView, QLabel, QVBoxLayout, QProgressBar, \
    QListWidgetItem, QMainWindow, QApplication, QFileDialog, QMessageBox, QDialog, QDialogButtonBox, QTreeWidget, \
    QTreeWidgetItem, QHeaderView, QHBoxLayout, QPushButton, QLineEdit

from control_manager import ControlManager
from models import TorrentState, TorrentInfo, FileTreeNode, FileInfo
from utils import humanize_speed, humanize_time, humanize_size


logging.basicConfig(format='%(levelname)s %(asctime)s %(name)-23s %(message)s', datefmt='%H:%M:%S')


STATE_FILENAME = 'state.bin'


ICON_DIRECTORY = os.path.join(os.path.dirname(__file__), 'icons')


def load_icon(name: str):
    return QIcon(os.path.join(ICON_DIRECTORY, name + '.svg'))


file_icon = load_icon('file')
directory_icon = load_icon('directory')


class TorrentAddingDialog(QDialog):
    SELECTION_LABEL_FORMAT = 'Selected {} files ({})'

    selected_download_dir = os.getcwd()

    def _traverse_file_tree(self, name: str, node: FileTreeNode, parent: QWidget):
        item = QTreeWidgetItem(parent)
        item.setCheckState(0, Qt.Checked)
        item.setText(0, name)
        if isinstance(node, FileInfo):
            item.setText(1, humanize_size(node.length))
            item.setIcon(0, file_icon)
            self._file_items.append((node, item))
            return

        item.setIcon(0, directory_icon)
        for name, child in node.items():
            self._traverse_file_tree(name, child, item)

    def _get_directory_browse_widget(self):
        widget = QWidget()
        hbox = QHBoxLayout(widget)
        hbox.setContentsMargins(0, 0, 0, 0)

        self._path_edit = QLineEdit(TorrentAddingDialog.selected_download_dir)
        self._path_edit.setReadOnly(True)
        hbox.addWidget(self._path_edit, 3)

        browse_button = QPushButton('Browse...')
        browse_button.clicked.connect(self._browse)
        hbox.addWidget(browse_button, 1)

        widget.setLayout(hbox)
        return widget

    def _browse(self):
        new_download_dir = QFileDialog.getExistingDirectory(self, 'Select download directory',
                                                            TorrentAddingDialog.selected_download_dir)
        if not new_download_dir:
            return

        TorrentAddingDialog.selected_download_dir = new_download_dir
        self._path_edit.setText(new_download_dir)

    def __init__(self, parent: QWidget, filename: str, torrent_info: TorrentInfo,
                 control_thread: 'ControlManagerThread'):
        super().__init__(parent)
        self._torrent_info = torrent_info
        download_info = torrent_info.download_info
        self._control_thread = control_thread

        vbox = QVBoxLayout(self)

        vbox.addWidget(QLabel('Download directory:'))
        vbox.addWidget(self._get_directory_browse_widget())

        vbox.addWidget(QLabel('Announce URLs:'))

        url_tree = QTreeWidget()
        url_tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        url_tree.header().close()
        vbox.addWidget(url_tree)
        for i, tier in enumerate(torrent_info.announce_list):
            tier_item = QTreeWidgetItem(url_tree)
            tier_item.setText(0, 'Tier {}'.format(i + 1))
            for url in tier:
                url_item = QTreeWidgetItem(tier_item)
                url_item.setText(0, url)
        url_tree.expandAll()
        vbox.addWidget(url_tree, 1)

        file_tree = QTreeWidget()
        file_tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        file_tree.setHeaderLabels(('Name', 'Size'))
        file_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._file_items = []
        self._traverse_file_tree(download_info.suggested_name, download_info.file_tree, file_tree)
        file_tree.sortItems(0, Qt.AscendingOrder)
        file_tree.expandAll()
        file_tree.itemClicked.connect(self._update_checkboxes)
        vbox.addWidget(file_tree, 3)

        self._selection_label = QLabel(TorrentAddingDialog.SELECTION_LABEL_FORMAT.format(
            len(download_info.files), humanize_size(download_info.total_size)))
        vbox.addWidget(self._selection_label)

        self._button_box = QDialogButtonBox(self)
        self._button_box.setOrientation(Qt.Horizontal)
        self._button_box.setStandardButtons(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self._button_box.button(QDialogButtonBox.Ok).clicked.connect(self.submit_torrent)
        self._button_box.button(QDialogButtonBox.Cancel).clicked.connect(self.close)
        vbox.addWidget(self._button_box)

        self.setFixedSize(450, 550)
        self.setWindowTitle('Adding "{}"'.format(filename))

    def _set_check_state_to_tree(self, item: QTreeWidgetItem, check_state: Qt.CheckState):
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, check_state)
            self._set_check_state_to_tree(child, check_state)

    def _update_checkboxes(self, item: QTreeWidgetItem, column: int):
        if column != 0:
            return

        new_check_state = item.checkState(0)
        self._set_check_state_to_tree(item, new_check_state)

        while True:
            item = item.parent()
            if item is None:
                break

            has_checked_children = False
            has_partially_checked_children = False
            has_unchecked_children = False
            for i in range(item.childCount()):
                state = item.child(i).checkState(0)
                if state == Qt.Checked:
                    has_checked_children = True
                elif state == Qt.PartiallyChecked:
                    has_partially_checked_children = True
                else:
                    has_unchecked_children = True

            if not has_partially_checked_children and not has_unchecked_children:
                new_state = Qt.Checked
            elif has_checked_children or has_partially_checked_children:
                new_state = Qt.PartiallyChecked
            else:
                new_state = Qt.Unchecked
            item.setCheckState(0, new_state)

        self._update_selection_label()

    def _update_selection_label(self):
        selected_file_count = 0
        selected_size = 0
        for node, item in self._file_items:
            if item.checkState(0) == Qt.Checked:
                selected_file_count += 1
                selected_size += node.length

        ok_button = self._button_box.button(QDialogButtonBox.Ok)
        if not selected_file_count:
            ok_button.setEnabled(False)
            self._selection_label.setText('Nothing to download')
        else:
            ok_button.setEnabled(True)
            self._selection_label.setText(TorrentAddingDialog.SELECTION_LABEL_FORMAT.format(
                selected_file_count, humanize_size(selected_size)))

    def submit_torrent(self):
        self._torrent_info.download_dir = TorrentAddingDialog.selected_download_dir

        file_paths = []
        for node, item in self._file_items:
            if item.checkState(0) == Qt.Checked:
                file_paths.append(node.path)
        if not self._torrent_info.download_info.single_file_mode:
            self._torrent_info.download_info.select_files(file_paths, 'whitelist')

        self._control_thread.loop.call_soon_threadsafe(self._control_thread.control.add, self._torrent_info)

        self.close()


class TorrentWidgetItem(QWidget):
    _name_font = QFont()
    _name_font.setBold(True)

    _stats_font = QFont()
    _stats_font.setPointSize(10)

    def __init__(self):
        super().__init__()
        vbox = QVBoxLayout(self)

        self._name_label = QLabel()
        self._name_label.setFont(TorrentWidgetItem._name_font)
        vbox.addWidget(self._name_label)

        self._upper_status_label = QLabel()
        self._upper_status_label.setFont(TorrentWidgetItem._stats_font)
        vbox.addWidget(self._upper_status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(15)
        self._progress_bar.setMaximum(1000)
        vbox.addWidget(self._progress_bar)

        self._lower_status_label = QLabel()
        self._lower_status_label.setFont(TorrentWidgetItem._stats_font)
        vbox.addWidget(self._lower_status_label)

        self._state = None

    @property
    def state(self) -> TorrentState:
        return self._state

    @state.setter
    def state(self, state: TorrentState):
        self._state = state

        self._name_label.setText(state.suggested_name)  # FIXME: Avoid XSS in all setText calls

        if state.downloaded_size < state.selected_size:
            status_text = '{} of {}'.format(humanize_size(state.downloaded_size), humanize_size(state.selected_size))
        else:
            status_text = '{} (complete)'.format(humanize_size(state.selected_size))
        status_text += ', Ratio: {:.1f}'.format(state.ratio)
        self._upper_status_label.setText(status_text)

        self._progress_bar.setValue(floor(state.progress * 1000))

        if state.paused:
            status_text = 'Paused'
        elif state.complete:
            status_text = 'Uploading to {} of {} peers'.format(state.uploading_peer_count, state.total_peer_count)
            if state.upload_speed:
                status_text += ' on {}'.format(humanize_speed(state.upload_speed))
        else:
            status_text = 'Downloading from {} of {} peers'.format(
                state.downloading_peer_count, state.total_peer_count)
            if state.download_speed:
                status_text += ' on {}'.format(humanize_speed(state.download_speed))
            eta_seconds = state.eta_seconds
            if eta_seconds is not None:
                status_text += ', {} remaining'.format(humanize_time(eta_seconds) if eta_seconds is not None else None)
        self._lower_status_label.setText(status_text)


class MainWindow(QMainWindow):
    def __init__(self, control_thread: 'ControlManagerThread'):
        super().__init__()

        self._control_thread = control_thread
        control = control_thread.control

        toolbar = self.addToolBar('Exits')
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        toolbar.setMovable(False)

        self._add_action = toolbar.addAction(load_icon('add'), 'Add')
        self._add_action.triggered.connect(self._add_torrent_triggered)

        self._pause_action = toolbar.addAction(load_icon('pause'), 'Pause')
        self._pause_action.setEnabled(False)
        self._pause_action.triggered.connect(partial(self._control_action_triggered, control.pause))

        self._resume_action = toolbar.addAction(load_icon('resume'), 'Resume')
        self._resume_action.setEnabled(False)
        self._resume_action.triggered.connect(partial(self._control_action_triggered, control.resume))

        self._remove_action = toolbar.addAction(load_icon('remove'), 'Remove')
        self._remove_action.setEnabled(False)
        self._remove_action.triggered.connect(partial(self._control_action_triggered, control.remove))

        self._about_action = toolbar.addAction(load_icon('about'), 'About')
        self._about_action.triggered.connect(self._show_about)

        self._list_widget = QListWidget()
        self._list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._list_widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._list_widget.itemSelectionChanged.connect(self._update_control_action_state)
        self._torrent_to_item = {}  # type: Dict[bytes, QListWidgetItem]

        self.setCentralWidget(self._list_widget)

        self.setMinimumSize(600, 500)
        self.setWindowTitle('BitTorrent Client')

        control.torrent_added.connect(self._add_torrent_item)
        control.torrent_changed.connect(self._update_torrent_item)
        control.torrent_removed.connect(self._remove_torrent_item)

        self.show()

    def _add_torrent_item(self, state: TorrentState):
        widget = TorrentWidgetItem()
        widget.state = state

        item = QListWidgetItem()
        item.setIcon(file_icon if state.single_file_mode else directory_icon)
        item.setSizeHint(widget.sizeHint())
        item.setData(Qt.UserRole, state.info_hash)

        items_upper = 0
        for i in range(self._list_widget.count()):
            prev_item = self._list_widget.item(i)
            if self._list_widget.itemWidget(prev_item).state.suggested_name > state.suggested_name:
                break
            items_upper += 1
        self._list_widget.insertItem(items_upper, item)

        self._list_widget.setItemWidget(item, widget)
        self._torrent_to_item[state.info_hash] = item

    def _update_torrent_item(self, state: TorrentState):
        widget = self._list_widget.itemWidget(self._torrent_to_item[state.info_hash])
        widget.state = state

        self._update_control_action_state()

    def _remove_torrent_item(self, info_hash: bytes):
        item = self._torrent_to_item[info_hash]
        self._list_widget.takeItem(self._list_widget.row(item))
        del self._torrent_to_item[info_hash]

        self._update_control_action_state()

    def _update_control_action_state(self):
        self._pause_action.setEnabled(False)
        self._resume_action.setEnabled(False)
        self._remove_action.setEnabled(False)
        for item in self._list_widget.selectedItems():
            state = self._list_widget.itemWidget(item).state
            if state.paused:
                self._resume_action.setEnabled(True)
            else:
                self._pause_action.setEnabled(True)
            self._remove_action.setEnabled(True)

    def _add_torrent_triggered(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Add torrent', filter='Torrent file (*.torrent)')
        if not filename:
            return

        try:
            torrent_info = TorrentInfo.from_file(filename, download_dir=None)

            if torrent_info.download_info.info_hash in self._torrent_to_item:
                raise ValueError('This torrent is already added')
        except Exception as err:
            QMessageBox.critical(self, 'Failed to add torrent', str(err))
            return

        TorrentAddingDialog(self, filename, torrent_info, self._control_thread).exec()

    async def _invoke_control_action(self, action, info_hash: bytes):
        try:
            result = action(info_hash)
            if asyncio.iscoroutine(result):
                await result
        except ValueError:
            pass

    def _control_action_triggered(self, action):
        for item in self._list_widget.selectedItems():
            info_hash = item.data(Qt.UserRole)
            asyncio.run_coroutine_threadsafe(self._invoke_control_action(action, info_hash), self._control_thread.loop)

    def _show_about(self):
        QMessageBox.about(self, 'About', '<p><b>Prototype of BitTorrent client</b></p>'
                                         '<p>Copyright (c) 2016 Alexander Borzunov</p>'
                                         '<p>Icons made by Google and Freepik from '
                                         '<a href="http://www.flaticon.com">www.flaticon.com</a></p>')


class ControlManagerThread(QThread):
    def __init__(self, control: ControlManager):
        super().__init__()

        self._loop = None  # type: asyncio.AbstractEventLoop
        self._control = control
        self._stopping = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def control(self) -> ControlManager:
        return self._control

    def _load_state(self):
        if os.path.isfile(STATE_FILENAME):
            with open(STATE_FILENAME, 'rb') as f:
                self._control.load(f)

    def _save_state(self):
        with open(STATE_FILENAME, 'wb') as f:
            self._control.dump(f)

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        with closing(self._loop):
            self._loop.run_until_complete(self._control.start())

            self._load_state()

            try:
                self._loop.run_forever()
            finally:
                self._save_state()

    def stop(self):
        if self._stopping:
            return
        self._stopping = True

        stop_fut = asyncio.run_coroutine_threadsafe(self._control.stop(), self._loop)
        stop_fut.add_done_callback(lambda fut: self._loop.stop())

        self.wait()


def main():
    parser = argparse.ArgumentParser(description='A prototype of BitTorrent client (GUI)')
    parser.add_argument('--debug', action='store_true', help='Show debug messages')
    args = parser.parse_args()

    if not args.debug:
        logging.disable(logging.INFO)

    app = QApplication(sys.argv)
    app.setWindowIcon(load_icon('logo'))

    control = ControlManager()
    control_thread = ControlManagerThread(control)
    control_thread.start()

    app.lastWindowClosed.connect(control_thread.stop)
    main_window = MainWindow(control_thread)
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
