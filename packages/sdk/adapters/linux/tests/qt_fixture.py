"""Qt accessibility fixture with an independent text-change journal."""

import json
import sys

from PyQt5.QtWidgets import QApplication, QLineEdit

app = QApplication([])
entry = QLineEdit()
entry.setWindowTitle("mai-ui Qt fixture")
entry.setAccessibleName("Qt fixture input")
entry.resize(400, 80)


def changed(text):
    with open(sys.argv[1], "a") as f:
        f.write(json.dumps({"text": text}) + "\n")


entry.textChanged.connect(changed)
entry.show()
app.exec_()
