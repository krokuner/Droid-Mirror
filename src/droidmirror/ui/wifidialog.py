"""The 'Connect wirelessly' address prompt.

Replaces a bare text input with the full ask: an editable address field and
a history dropdown that slides open with every address that has connected
before, so a known device is one pick away instead of a retype. Selecting a
history entry drops it straight into the field.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


class WifiConnectDialog(QDialog):
    """Prompt for the device address, with the past IPs one slide away."""

    def __init__(
        self,
        parent=None,
        *,
        prefill: str = "",
        history: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect wirelessly")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        heading = QLabel("Device address (IP, or IP:port)")
        layout.addWidget(heading)
        tip = QLabel(
            "Find it on the tablet under Settings > Wi-Fi > (current "
            "network) > IP address."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #7d8299; font-size: 12px;")
        layout.addWidget(tip)

        self._address = QLineEdit(prefill)
        self._address.setPlaceholderText("192.168.1.42")
        self._address.selectAll()
        self._address.returnPressed.connect(self.accept)

        self._history = QComboBox()
        self._history.setMinimumWidth(150)
        self._history.setToolTip(
            "Addresses that connected before - click to slide the list open"
        )
        entries = [str(h) for h in (history or []) if h]
        if entries:
            self._history.addItems(entries)
            self._history.setPlaceholderText("History \u25be")
            self._history.setCurrentIndex(-1)  # placeholder until picked
            # activated, not currentIndexChanged: only a deliberate pick
            # overwrites whatever is typed.
            self._history.activated.connect(self._on_history_pick)
        else:
            self._history.hide()

        row = QVBoxLayout()
        row.addWidget(self._address, 1)
        row.addWidget(self._history, 0)
        layout.addLayout(row)

        # Android 11+ "Wireless debugging" refuses unpaired computers, so a
        # first connection needs the pairing IP:port and 6-digit code from
        # "Pair device with pairing code". Blank = just connect.
        pair_heading = QLabel("First time on Wi-Fi? Pair with a code")
        pair_heading.setStyleSheet("margin-top: 8px;")
        layout.addWidget(pair_heading)
        pair_tip = QLabel(
            "On the tablet: Developer options > Wireless debugging > Pair "
            "device with pairing code. Keep the popup open and type just the "
            "6-digit code - its IP:port is found automatically. Leave blank "
            "if this computer is already paired."
        )
        pair_tip.setWordWrap(True)
        pair_tip.setStyleSheet("color: #7d8299; font-size: 12px;")
        layout.addWidget(pair_tip)

        pair_row = QHBoxLayout()
        self._pair_address = QLineEdit()
        self._pair_address.setPlaceholderText("Pairing IP:port (auto-detected)")
        self._pair_code = QLineEdit()
        self._pair_code.setPlaceholderText("Code")
        self._pair_code.setMaxLength(10)
        self._pair_code.setMaximumWidth(110)
        self._pair_code.returnPressed.connect(self.accept)
        pair_row.addWidget(self._pair_address, 1)
        pair_row.addWidget(self._pair_code, 0)
        layout.addLayout(pair_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_history_pick(self, index: int) -> None:
        """A slide-down pick replaces the field (and selects it for edits)."""
        self._address.setText(self._history.itemText(index))
        self._address.selectAll()

    def address(self) -> str:
        """The field's contents, stripped. Empty means 'nothing to dial'."""
        return self._address.text().strip()

    def pairing(self) -> tuple[str, str] | None:
        """``(pair_address, code)`` if a pairing code was entered, else None.

        The pairing address may be just a port (``37099``) or omitted, in
        which case the host is taken from the connect address.
        """
        code = self._pair_code.text().strip().replace(" ", "").replace("-", "")
        if not code:
            return None
        pair_addr = self._pair_address.text().strip()
        host = self.address().split(":", 1)[0]
        if pair_addr.isdigit() and host:
            pair_addr = f"{host}:{pair_addr}"
        return pair_addr, code
