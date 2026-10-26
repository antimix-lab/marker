#!/usr/bin/env python3
"""Interfaccia desktop per convertire PDF in Markdown con marker-pdf."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QDragEnterEvent, QDropEvent, QIntValidator, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


class AnimatedProgressBar(QProgressBar):
    """Barra determinata con una fascia luminosa mobile durante il lavoro."""

    def __init__(self) -> None:
        super().__init__()
        self._busy = False
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(45)
        self._timer.timeout.connect(self._advance_animation)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _advance_animation(self) -> None:
        self._phase = (self._phase + 7) % max(1, self.width() + 90)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        if not self._busy:
            return
        painter = QPainter(self)
        painter.setOpacity(0.75)
        stripe_width = 70
        x = self._phase - stripe_width
        painter.fillRect(x, 2, stripe_width, max(0, self.height() - 4), QColor("#55c8ff"))


class PdfDropList(QListWidget):
    """Lista che accetta file PDF trascinati dal file manager."""

    files_dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setAlternatingRowColors(True)
        self.setMinimumHeight(150)
        self.setToolTip("Trascina qui uno o più file PDF")

    @staticmethod
    def _has_local_pdf(event: QDragEnterEvent | QDropEvent) -> bool:
        return any(
            url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() == ".pdf"
            for url in event.mimeData().urls()
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and self._has_local_pdf(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls() and self._has_local_pdf(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        self.files_dropped.emit(paths)
        event.acceptProposedAction()


class ConversionWorker(QObject):
    """Esegue marker_single fuori dal thread grafico."""

    log = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal(bool, int, int, bool)

    def __init__(
        self,
        pdfs: list[Path],
        output_dir: Path | None,
        marker_executable: Path,
        use_tesseract: bool,
        ocrmypdf_executable: Path | None,
        first_page: int | None,
        last_page: int | None,
    ) -> None:
        super().__init__()
        self.pdfs = pdfs
        self.output_dir = output_dir
        self.marker_executable = marker_executable
        self.use_tesseract = use_tesseract
        self.ocrmypdf_executable = ocrmypdf_executable
        self.first_page = first_page
        self.last_page = last_page
        self._cancel_requested = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()

    def _emit_process_output(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk).replace("\r", "\n")
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                if line.strip():
                    self.log.emit(line.rstrip())
        pending += decoder.decode(b"", final=True)
        if pending.strip():
            self.log.emit(pending.rstrip())

    def _run_process(self, command: list[str], environment: dict[str, str]) -> int:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            bufsize=0,
            start_new_session=True,
        )
        with self._process_lock:
            self._process = process
        self._emit_process_output(process)
        return_code = process.wait()
        with self._process_lock:
            self._process = None
        return return_code

    @staticmethod
    def _format_elapsed(started_at: float) -> str:
        elapsed = max(0, round(time.monotonic() - started_at))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    @staticmethod
    def _pdf_page_count(pdf: Path) -> int:
        result = subprocess.run(
            ["pdfinfo", str(pdf)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "pdfinfo non è riuscito a leggere il PDF")
        match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, flags=re.MULTILINE)
        if match is None:
            raise RuntimeError("pdfinfo non ha restituito il numero di pagine")
        return int(match.group(1))

    @staticmethod
    def _repair_split_numeric_columns(markdown_path: Path) -> int:
        """Riunisce colonne spurie create quando Marker spezza un importo."""
        if not markdown_path.is_file():
            return 0
        lines = markdown_path.read_text(encoding="utf-8").splitlines()
        repaired = 0
        index = 0
        while index < len(lines):
            if not lines[index].strip().startswith("|"):
                index += 1
                continue
            end = index
            while end < len(lines) and lines[end].strip().startswith("|"):
                end += 1
            block = lines[index:end]
            rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in block]
            if len(rows) < 3 or len({len(row) for row in rows}) != 1:
                index = end
                continue
            separators = rows[1]
            if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separators):
                index = end
                continue
            merge_columns = []
            for column in range(1, len(rows[0])):
                continuations = sum(
                    bool(re.fullmatch(r"\d+,\d{2}", row[column].replace(" ", "")))
                    and bool(re.search(r"\d$", row[column - 1]))
                    for row in rows[2:]
                )
                if not rows[0][column] and continuations >= max(2, len(rows[2:]) // 2):
                    merge_columns.append(column)
            if merge_columns:
                for column in reversed(merge_columns):
                    for row_number, row in enumerate(rows):
                        if row_number != 1 and row[column]:
                            row[column - 1] = f"{row[column - 1]}{row[column]}"
                        del row[column]
                lines[index:end] = ["| " + " | ".join(row) + " |" for row in rows]
                repaired += len(merge_columns)
                end = index + len(rows)
            index = end
        if repaired:
            markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return repaired

    def run(self) -> None:
        completed = 0
        total = len(self.pdfs)
        all_ok = True
        file_started: float | None = None

        try:
            for index, pdf in enumerate(self.pdfs, start=1):
                if self._cancel_requested.is_set():
                    all_ok = False
                    break

                destination = self.output_dir or pdf.parent
                file_started = time.monotonic()
                destination.mkdir(parents=True, exist_ok=True)
                self.progress.emit(index, total, pdf.name)
                self.log.emit("")
                self.log.emit(f"[{index}/{total}] Conversione di: {pdf}")
                self.log.emit(f"Destinazione: {destination}")

                environment = os.environ.copy()
                environment["PYTHONUNBUFFERED"] = "1"

                marker_page_range: str | None = None
                ocr_page_range: str | None = None
                if self.first_page is not None:
                    page_count = self._pdf_page_count(pdf)
                    effective_last = self.last_page or page_count
                    if self.first_page > page_count:
                        all_ok = False
                        self.log.emit(
                            f"ERRORE: la pagina iniziale {self.first_page} supera le "
                            f"{page_count} pagine di {pdf.name}."
                        )
                        self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
                        file_started = None
                        continue
                    if effective_last > page_count:
                        all_ok = False
                        self.log.emit(
                            f"ERRORE: la pagina finale {effective_last} supera le "
                            f"{page_count} pagine di {pdf.name}."
                        )
                        self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
                        file_started = None
                        continue
                    marker_page_range = f"{self.first_page - 1}-{effective_last - 1}"
                    ocr_page_range = f"{self.first_page}-{effective_last}"
                    self.log.emit(
                        f"Intervallo: pagine {self.first_page}-{effective_last} su {page_count}."
                    )

                with tempfile.TemporaryDirectory(prefix="marker-pdf-gui-") as temp_dir:
                    marker_input = pdf
                    marker_extra: list[str] = []
                    if self.use_tesseract:
                        if self.ocrmypdf_executable is None:
                            all_ok = False
                            self.log.emit("ERRORE: ocrmypdf non è installato.")
                            self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
                            file_started = None
                            continue
                        # --redo-ocr conserva il testo vettoriale visibile e applica
                        # OCR alle immagini, senza euristiche o pagine prestabilite.
                        marker_input = Path(temp_dir) / pdf.name
                        ocr_command = [
                            str(self.ocrmypdf_executable),
                            "--redo-ocr",
                            "--output-type",
                            "pdf",
                            "--optimize",
                            "0",
                            "--jobs",
                            str(max(1, min(4, os.cpu_count() or 1))),
                            "--language",
                            "ita+eng",
                        ]
                        if ocr_page_range is not None:
                            ocr_command.extend(["--pages", ocr_page_range])
                        ocr_command.extend([str(pdf), str(marker_input)])
                        self.log.emit(
                            "Fase 1/2: OCR conservativo delle immagini; il testo digitale resta invariato."
                        )
                        self.log.emit(f"Comando OCR: {shlex.join(ocr_command)}")
                        ocr_return_code = self._run_process(ocr_command, environment)
                        if self._cancel_requested.is_set():
                            all_ok = False
                            self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
                            file_started = None
                            break
                        if ocr_return_code != 0:
                            all_ok = False
                            self.log.emit(
                                f"ERRORE: OCRmyPDF è terminato con codice {ocr_return_code}."
                            )
                            self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
                            file_started = None
                            continue
                        marker_extra = ["--disable_ocr"]
                        self.log.emit("Fase 2/2: Marker struttura il PDF e genera Markdown.")
                    else:
                        self.log.emit(
                            "OCR AI Marker: qualità superiore, ma le scansioni possono richiedere molte ore su CPU."
                        )

                    command = [
                        str(self.marker_executable),
                        str(marker_input),
                        "--output_dir",
                        str(destination),
                        "--output_format",
                        "markdown",
                        "--mode",
                        "fast",
                        # Con --disable_ocr Marker non ha un fallback VLM: accetta
                        # quindi le tabelle ricostruite dal testo PDF anche quando
                        # il punteggio euristico è basso, evitando celle eliminate.
                        "--min_recon_score",
                        "0",
                        *marker_extra,
                    ]
                    if marker_page_range is not None:
                        command.extend(["--page_range", marker_page_range])
                    self.log.emit(f"Comando Marker: {shlex.join(command)}")
                    return_code = self._run_process(command, environment)

                if self._cancel_requested.is_set():
                    all_ok = False
                    self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
                    file_started = None
                    break
                if return_code != 0:
                    all_ok = False
                    self.log.emit(f"ERRORE: marker_single è terminato con codice {return_code}.")
                else:
                    markdown_path = destination / pdf.stem / f"{pdf.stem}.md"
                    repaired = self._repair_split_numeric_columns(markdown_path)
                    if repaired:
                        self.log.emit(
                            f"Corrette {repaired} colonne numeriche spezzate nelle tabelle."
                        )
                    completed += 1
                    self.log.emit(f"Completato: {pdf.name}")
                self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
                file_started = None
        except Exception as error:  # mostra nella GUI anche errori inattesi del worker
            all_ok = False
            self.log.emit(f"ERRORE IMPREVISTO: {type(error).__name__}: {error}")
            if file_started is not None:
                self.log.emit(f"Tempo impiegato: {self._format_elapsed(file_started)}")
        finally:
            cancelled = self._cancel_requested.is_set()
            self.finished.emit(all_ok and completed == total, completed, total, cancelled)


class MarkerWindow(QMainWindow):
    def __init__(self, initial_files: list[str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Marker PDF → Markdown")
        self.resize(820, 650)

        self.settings = QSettings("MarkerPDF", "MarkerPDFGUI")
        saved_destination = self.settings.value("output_directory", "", type=str)
        saved_path = Path(saved_destination).expanduser() if saved_destination else None
        self.custom_output_dir: Path | None = (
            saved_path.resolve() if saved_path is not None and saved_path.is_dir() else None
        )
        if saved_destination and self.custom_output_dir is None:
            self.settings.remove("output_directory")
        self.thread: QThread | None = None
        self.worker: ConversionWorker | None = None
        self.close_after_worker = False

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(10)

        title = QLabel("Converti PDF in Markdown, in locale")
        title.setObjectName("title")
        subtitle = QLabel(
            "Trascina i PDF nell’area sottostante. Al primo utilizzo Marker può scaricare i modelli AI."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.drop_hint = QLabel("Rilascia qui uno o più PDF")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setObjectName("dropHint")
        layout.addWidget(self.drop_hint)

        self.pdf_list = PdfDropList()
        self.pdf_list.files_dropped.connect(self.add_files)
        layout.addWidget(self.pdf_list)

        file_buttons = QHBoxLayout()
        self.browse_button = QPushButton("Sfoglia…")
        self.remove_button = QPushButton("Rimuovi selezionati")
        self.clear_button = QPushButton("Svuota elenco")
        self.browse_button.clicked.connect(self.browse_pdfs)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button.clicked.connect(self.pdf_list.clear)
        file_buttons.addWidget(self.browse_button)
        file_buttons.addWidget(self.remove_button)
        file_buttons.addWidget(self.clear_button)
        file_buttons.addStretch()
        layout.addLayout(file_buttons)

        destination_label = QLabel("Cartella di destinazione")
        layout.addWidget(destination_label)
        destination_row = QHBoxLayout()
        self.destination_edit = QLineEdit()
        self.destination_edit.setReadOnly(True)
        self.destination_edit.setText(
            str(self.custom_output_dir)
            if self.custom_output_dir is not None
            else "Cartella di origine di ciascun PDF (predefinita)"
        )
        self.destination_button = QPushButton("Scegli…")
        self.source_button = QPushButton("Usa cartella origine")
        self.destination_button.clicked.connect(self.choose_destination)
        self.source_button.clicked.connect(self.use_source_folders)
        destination_row.addWidget(self.destination_edit, 1)
        destination_row.addWidget(self.destination_button)
        destination_row.addWidget(self.source_button)
        layout.addLayout(destination_row)

        page_row = QHBoxLayout()
        self.all_pages_radio = QRadioButton("Tutto il documento")
        self.page_range_radio = QRadioButton("Da pagina")
        self.first_page_edit = QLineEdit()
        self.first_page_edit.setPlaceholderText("iniziale")
        self.first_page_edit.setMaximumWidth(80)
        self.first_page_edit.setValidator(QIntValidator(1, 999999, self))
        self.last_page_label = QLabel("a pagina")
        self.last_page_edit = QLineEdit()
        self.last_page_edit.setPlaceholderText("ultima")
        self.last_page_edit.setToolTip("Lascia vuoto per arrivare fino all’ultima pagina")
        self.last_page_edit.setMaximumWidth(80)
        self.last_page_edit.setValidator(QIntValidator(1, 999999, self))
        self.all_pages_radio.setChecked(True)
        self.page_range_radio.toggled.connect(self.update_page_range_controls)
        page_row.addWidget(self.all_pages_radio)
        page_row.addSpacing(12)
        page_row.addWidget(self.page_range_radio)
        page_row.addWidget(self.first_page_edit)
        page_row.addWidget(self.last_page_label)
        page_row.addWidget(self.last_page_edit)
        page_row.addWidget(QLabel("(vuota = ultima)"))
        page_row.addStretch()
        layout.addLayout(page_row)
        self.update_page_range_controls(False)

        self.tesseract_checkbox = QCheckBox(
            "Pre-OCR rapido con Tesseract (consigliato su CPU)"
        )
        self.tesseract_checkbox.setChecked(True)
        self.tesseract_checkbox.setToolTip(
            "OCRmyPDF aggiunge un livello di testo alle scansioni; Marker evita poi il lento OCR AI."
        )
        layout.addWidget(self.tesseract_checkbox)

        action_row = QHBoxLayout()
        self.convert_button = QPushButton("Converti")
        self.convert_button.setObjectName("primaryButton")
        self.cancel_button = QPushButton("Annulla")
        self.cancel_button.setEnabled(False)
        self.convert_button.clicked.connect(self.start_conversion)
        self.cancel_button.clicked.connect(self.cancel_conversion)
        self.status_label = QLabel("Pronto")
        action_row.addWidget(self.convert_button)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.status_label, 1)
        layout.addLayout(action_row)

        self.progress = AnimatedProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        layout.addWidget(QLabel("Log"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("Qui compariranno download, avanzamento ed eventuali errori…")
        layout.addWidget(self.log_view, 1)

        self.setStyleSheet(
            """
            QMainWindow { background: #f5f6f7; }
            QLabel#title { font-size: 22px; font-weight: 600; color: #203040; }
            QLabel#dropHint {
                border: 2px dashed #5586b3; border-radius: 8px;
                padding: 16px; color: #315d83; font-size: 16px; background: #eef6fc;
            }
            QPushButton { padding: 7px 12px; }
            QPushButton#primaryButton { background: #2878b5; color: white; font-weight: 600; }
            QPlainTextEdit { background: #17202a; color: #e8edf2; font-family: monospace; }
            """
        )

        if initial_files:
            self.add_files(initial_files)

    def selected_pdfs(self) -> list[Path]:
        return [Path(self.pdf_list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.pdf_list.count())]

    def add_files(self, paths: list[str]) -> None:
        existing = {str(path.resolve()) for path in self.selected_pdfs()}
        rejected: list[str] = []
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if not path.is_file() or path.suffix.lower() != ".pdf":
                rejected.append(str(path))
                continue
            resolved = str(path.resolve())
            if resolved in existing:
                continue
            item = QListWidgetItem(path.name)
            item.setToolTip(resolved)
            item.setData(Qt.ItemDataRole.UserRole, resolved)
            self.pdf_list.addItem(item)
            existing.add(resolved)
        if rejected:
            self.append_log("Ignorati (non sono PDF validi): " + ", ".join(rejected))

    def browse_pdfs(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Seleziona file PDF", str(Path.home()), "File PDF (*.pdf)")
        self.add_files(paths)

    def remove_selected(self) -> None:
        for item in self.pdf_list.selectedItems():
            self.pdf_list.takeItem(self.pdf_list.row(item))

    def choose_destination(self) -> None:
        start = str(self.custom_output_dir or Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Scegli la cartella di destinazione", start)
        if selected:
            self.custom_output_dir = Path(selected).resolve()
            self.destination_edit.setText(str(self.custom_output_dir))
            self.settings.setValue("output_directory", str(self.custom_output_dir))

    def use_source_folders(self) -> None:
        self.custom_output_dir = None
        self.destination_edit.setText("Cartella di origine di ciascun PDF (predefinita)")
        self.settings.remove("output_directory")

    def update_page_range_controls(self, enabled: bool) -> None:
        self.first_page_edit.setEnabled(enabled)
        self.last_page_label.setEnabled(enabled)
        self.last_page_edit.setEnabled(enabled)
        if enabled:
            self.first_page_edit.setFocus()

    def marker_executable(self) -> Path | None:
        beside_python = Path(sys.executable).resolve().with_name("marker_single")
        if beside_python.is_file() and os.access(beside_python, os.X_OK):
            return beside_python
        found = shutil.which("marker_single")
        return Path(found) if found else None

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.pdf_list,
            self.browse_button,
            self.remove_button,
            self.clear_button,
            self.destination_button,
            self.source_button,
            self.all_pages_radio,
            self.page_range_radio,
            self.first_page_edit,
            self.last_page_edit,
            self.tesseract_checkbox,
            self.convert_button,
        ):
            widget.setEnabled(enabled)
        self.update_page_range_controls(enabled and self.page_range_radio.isChecked())
        self.cancel_button.setEnabled(not enabled)

    def start_conversion(self) -> None:
        pdfs = self.selected_pdfs()
        if not pdfs:
            QMessageBox.information(self, "Nessun PDF", "Aggiungi almeno un file PDF da convertire.")
            return
        executable = self.marker_executable()
        if executable is None:
            QMessageBox.critical(
                self,
                "Marker non trovato",
                "L’eseguibile marker_single non è disponibile. Esegui di nuovo install_marker.sh.",
            )
            return
        ocrmypdf = shutil.which("ocrmypdf") if self.tesseract_checkbox.isChecked() else None
        if self.tesseract_checkbox.isChecked() and ocrmypdf is None:
            QMessageBox.critical(
                self,
                "OCRmyPDF non trovato",
                "La modalità Tesseract richiede OCRmyPDF. Esegui nuovamente install_marker.sh.",
            )
            return

        first_page: int | None = None
        last_page: int | None = None
        if self.page_range_radio.isChecked():
            first_text = self.first_page_edit.text().strip()
            last_text = self.last_page_edit.text().strip()
            if not first_text:
                QMessageBox.information(
                    self, "Pagina iniziale mancante", "Indica la prima pagina da convertire."
                )
                self.first_page_edit.setFocus()
                return
            first_page = int(first_text)
            last_page = int(last_text) if last_text else None
            if last_page is not None and last_page < first_page:
                QMessageBox.information(
                    self,
                    "Intervallo non valido",
                    "La pagina finale non può precedere quella iniziale.",
                )
                self.last_page_edit.setFocus()
                return

        self.log_view.clear()
        self.append_log(f"Avvio di {len(pdfs)} conversione/i con {executable}")
        self.set_controls_enabled(False)
        self.progress.setRange(0, 0)
        self.progress.set_busy(True)
        self.status_label.setText("Avvio…")

        self.thread = QThread(self)
        self.worker = ConversionWorker(
            pdfs,
            self.custom_output_dir,
            executable,
            self.tesseract_checkbox.isChecked(),
            Path(ocrmypdf) if ocrmypdf else None,
            first_page,
            last_page,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.conversion_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.release_thread)
        self.thread.start()

    def cancel_conversion(self) -> None:
        if self.worker is not None:
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Annullamento…")
            self.append_log("Annullamento richiesto…")
            self.worker.request_cancel()

    def append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def update_progress(self, current: int, total: int, filename: str) -> None:
        self.status_label.setText(f"{current}/{total} — {filename}")

    def conversion_finished(self, success: bool, completed: int, total: int, cancelled: bool) -> None:
        self.progress.setRange(0, total or 1)
        self.progress.setValue(completed)
        self.progress.set_busy(False)
        self.set_controls_enabled(True)
        if cancelled:
            message = f"Conversione annullata ({completed}/{total} completate)."
        elif success:
            message = f"Conversione completata: {completed}/{total} PDF."
        else:
            message = f"Conversione terminata con errori: {completed}/{total} PDF completati."
        self.status_label.setText(message)
        self.append_log("")
        self.append_log(message)
        if success:
            QMessageBox.information(self, "Operazione completata", message)
        elif not cancelled:
            QMessageBox.warning(self, "Operazione terminata", message + "\nControlla il log per i dettagli.")

    def release_thread(self) -> None:
        self.thread = None
        self.worker = None
        if self.close_after_worker:
            self.close_after_worker = False
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.thread is not None and self.thread.isRunning():
            answer = QMessageBox.question(
                self,
                "Conversione in corso",
                "Vuoi annullare la conversione e chiudere?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.close_after_worker = True
                self.cancel_conversion()
            event.ignore()
            return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Marker PDF")
    app.setOrganizationName("Local Tools")
    initial_pdfs = [argument for argument in sys.argv[1:] if argument.lower().endswith(".pdf")]
    window = MarkerWindow(initial_pdfs)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
