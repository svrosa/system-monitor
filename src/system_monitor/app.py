from __future__ import annotations

from collections import deque

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal
from textual.widgets import DataTable, Footer, Input, Static
from rich.markup import escape

from .metrics import (
    NetworkSampler,
    cpu_metrics,
    disk_metrics,
    gpu_metrics,
    human_bytes,
    memory_metrics,
    prime_process_cpu,
    system_metrics,
    process_snapshot,
)


ACCENT = "#BB9AF7"
CYAN = "#7DCFFF"

DISK_EMPTY = "#BB9AF7"


def colored_bar(
    percent: float,
    *,
    width: int = 24,
    color: str = CYAN,
) -> str:
    percent = max(0.0, min(float(percent), 100.0))
    filled = round(width * percent / 100)

    used = "█" * filled
    empty = "░" * (width - filled)

    return (
        f"[{color}]{used}[/]"
        f"[{DISK_EMPTY}]{empty}[/]"
    )

GRAPH_WIDTH = 36

def history_graph(
    values,
    *,
    width: int = GRAPH_WIDTH,
    floor: float = 0.0,
    ceiling: float = 100.0,
) -> str:
    samples = list(values)[-width:]
    samples = [floor] * (width - len(samples)) + samples

    ceiling = max(float(ceiling), floor + 0.001)
    span = ceiling - floor

    # Non-filled levels: visually reads as history, not a progress bar.
    levels = " ▁▂▃▄▅▆▇"
    chars = []

    for value in samples:
        value = max(floor, min(float(value), ceiling))
        normalized = (value - floor) / span
        index = round(normalized * (len(levels) - 1))
        chars.append(levels[index])

    return "".join(chars)

def adaptive_history_graph(
    values,
    *,
    width: int = GRAPH_WIDTH,
    min_span: float = 20.0,
) -> str:
    samples = list(values)[-width:]

    if not samples:
        return " " * width

    low = min(samples)
    high = max(samples)

    # Give the graph breathing room above/below observed values.
    padding = max((high - low) * 0.20, 2.0)

    floor = max(0.0, low - padding)
    ceiling = min(100.0, high + padding)

    # Prevent tiny fluctuations from being massively exaggerated.
    if ceiling - floor < min_span:
        center = (ceiling + floor) / 2
        floor = max(0.0, center - min_span / 2)
        ceiling = min(100.0, floor + min_span)

        if ceiling - floor < min_span:
            floor = max(0.0, ceiling - min_span)

    return history_graph(
        samples,
        width=width,
        floor=floor,
        ceiling=ceiling,
    )

class SystemMonitor(App):
    CSS_PATH = "styles.tcss"
    TITLE = "System Monitor"
    AUTO_FOCUS = None

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "sort_cpu", "CPU"),
        Binding("g", "sort_gpu", "GPU"),
        Binding("m", "sort_memory", "Memory"),
        Binding("v", "sort_vram", "VRAM"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "search_processes", "Search"),
        Binding("escape", "clear_search", "Clear Search"),
        Binding("home", "process_home", "Top", show=False),
        Binding("end", "process_end", "Bottom", show=False),
        Binding("pageup", "process_page_up", "Page Up", show=False),
        Binding("pagedown", "process_page_down", "Page Down", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()

        self.process_rows: list[dict] = []
        self.process_filter = ""

        self.network = NetworkSampler()
        self.process_sort = "cpu"

        self.cpu_history: deque[float] = deque(maxlen=GRAPH_WIDTH)
        self.memory_history: deque[float] = deque(maxlen=GRAPH_WIDTH)
        self.gpu_history: deque[float] = deque(maxlen=GRAPH_WIDTH)
        self.download_history: deque[float] = deque(maxlen=GRAPH_WIDTH)
        self.upload_history: deque[float] = deque(maxlen=GRAPH_WIDTH)
        self.rendered_pids: set[str] = set()

    def update_process_cell(
        self,
        row_key: str,
        column_key: str,
        value: str,
    ) -> None:
        table = self.process_table

        if table.get_cell(row_key, column_key) != value:
            table.update_cell(
                row_key,
                column_key,
                value,
                update_width=False,
            )

    def compose(self) -> ComposeResult:
        with Horizontal(id="titlebar"):
            yield Static("SYSTEM MONITOR", id="title")
            yield Static("", id="uptime")
        with Grid(id="metrics-grid"):
            yield Static("", id="cpu", classes="metric")
            yield Static("", id="memory", classes="metric")
            yield Static("", id="gpu", classes="metric")
            yield Static("", id="network", classes="metric")
            yield Static("", id="disk", classes="metric")
            yield Static("", id="system", classes="metric")

        yield Static("TOP PROCESSES", id="process-title")
        yield Input(
            placeholder="Filter by process name or PID...",
            id="process-search",
        )
        yield DataTable(id="processes")

        yield Footer()

    def on_mount(self) -> None:
        self.cpu_widget = self.query_one("#cpu", Static)
        self.memory_widget = self.query_one("#memory", Static)
        self.gpu_widget = self.query_one("#gpu", Static)
        self.network_widget = self.query_one("#network", Static)
        self.disk_widget = self.query_one("#disk", Static)
        self.system_widget = self.query_one("#system", Static)
        self.uptime_widget = self.query_one("#uptime", Static)

        self.process_title_widget = self.query_one("#process-title", Static)
        self.process_search_widget = self.query_one("#process-search", Input)
        self.process_table = self.query_one("#processes", DataTable)

        table = self.process_table

        table.add_column("PID", width=10, key="pid")
        table.add_column("PROCESS", width=34, key="process")
        table.add_column("CPU", width=9, key="cpu")
        table.add_column("GPU", width=8, key="gpu")
        table.add_column("RAM", width=13, key="ram")
        table.add_column("VRAM", width=13, key="vram")

        table.cursor_type = "none"
        table.show_cursor = False
        table.zebra_stripes = False

        prime_process_cpu()

        self.refresh_fast_metrics()
        self.refresh_disks()

        self.set_timer(0.25, self.refresh_gpu)
        self.set_timer(0.50, self.refresh_processes)

        self.set_interval(1.0, self.refresh_fast_metrics)
        self.set_interval(2.0, self.refresh_gpu)
        self.set_interval(5.0, self.refresh_processes)
        self.set_interval(5.0, self.refresh_disks)

    def refresh_fast_metrics(self) -> None:
        cpu = cpu_metrics()
        memory = memory_metrics()        
        network = self.network.sample()
        system = system_metrics()

        self.cpu_history.append(cpu["percent"])
        self.memory_history.append(memory["percent"])

        self.download_history.append(network["download"])
        self.upload_history.append(network["upload"])
        

        self.uptime_widget.update(
            f"[dim]uptime[/] {system['uptime']}"
        )

        temp = (
            f"{cpu['temperature']:.0f}°C"
            if cpu["temperature"] is not None
            else "N/A"
        )

        frequency = (
            f"{cpu['frequency'] / 1000:.2f} GHz"
            if cpu["frequency"] is not None
            else "N/A"
        )

        core_text = "   ".join(
            f"C{i} {value:2.0f}%"
            for i, value in enumerate(cpu["per_core"])
        )

        self.cpu_widget.update(
            f"[bold {ACCENT}]CPU[/]\n"
            f"[bold]{cpu['percent']:5.1f}%[/]   {temp}   {frequency}\n"
            f"[{ACCENT}]{adaptive_history_graph(self.cpu_history)}[/]\n"
            f"[dim]{core_text}[/]"
        )

        self.memory_widget.update(
            f"[bold {ACCENT}]MEMORY[/]\n"
            f"[bold]{memory['percent']:5.1f}%[/]   "
            f"{human_bytes(memory['used'])} / {human_bytes(memory['total'])}\n"
            f"[{ACCENT}]{adaptive_history_graph(self.memory_history, min_span=30)}[/]\n"
            f"Swap {human_bytes(memory['swap_used'])} / "
            f"{human_bytes(memory['swap_total'])}"
        )

        
        network_peak = max(
            max(self.download_history, default=0),
            max(self.upload_history, default=0),
            1024,
        )

        download_text = f"{human_bytes(network['download'])}/s"
        upload_text = f"{human_bytes(network['upload'])}/s"
        peak_text = f"{human_bytes(network_peak)}/s"

        self.network_widget.update(
            f"[bold {ACCENT}]NETWORK[/]   "
            f"[dim]{network['interface']}[/]   "
            f"{network['ip']}\n"
            f"[{CYAN}]↓[/]  [bold]{download_text}[/]   "
            f"[dim]peak {peak_text}[/]\n"
            f"[{CYAN}]"
            f"{history_graph(self.download_history, floor=0, ceiling=network_peak)}"
            f"[/]\n"
            f"[{ACCENT}]↑[/]  [bold]{upload_text}[/]\n"
            f"[{ACCENT}]"
            f"{history_graph(self.upload_history, floor=0, ceiling=network_peak)}"
            f"[/]\n"
            f"[dim]RX {human_bytes(network['received_total'])}   "
            f"TX {human_bytes(network['sent_total'])}[/]"
        )

       
        
        load1, load5, load15 = system["load"]

        self.system_widget.update(
            f"[bold {ACCENT}]SYSTEM[/]\n"
            f"\n"
            f"Host        [bold]{system['hostname']}[/]\n"
            f"Kernel      {system['kernel']}\n"
            f"Processes   [bold]{system['processes']}[/]\n"
            f"Load        {load1:.2f}  {load5:.2f}  {load15:.2f}"
        )
        

    def refresh_gpu(self) -> None:
        gpu = gpu_metrics()

        if not gpu:
            self.gpu_widget.update(
                f"[bold {ACCENT}]GPU[/]\n\nNVIDIA GPU unavailable"
            )
            return

        self.gpu_history.append(gpu["percent"])

        self.gpu_widget.update(
            f"[bold {ACCENT}]GPU[/]\n"
            f"[bold]{gpu['percent']:5.1f}%[/]   "
            f"{gpu['temperature']:.0f}°C   {gpu['name']}\n"
            f"[{ACCENT}]{adaptive_history_graph(self.gpu_history)}[/]\n"
            f"VRAM {human_bytes(gpu['memory_used'])} / "
            f"{human_bytes(gpu['memory_total'])}"
        )


    def refresh_disks(self) -> None:
        disks = disk_metrics()

        lines = [f"[bold {ACCENT}]DISK[/]"]

        for disk in disks:
            lines.extend(
                [
                    f"{disk['name']}  [bold]{disk['percent']:.0f}%[/]",
                    colored_bar(disk["percent"], color=CYAN),
                    f"{human_bytes(disk['used'])} / "
                    f"{human_bytes(disk['total'])}",
                ]
            )

        self.disk_widget.update("\n".join(lines))

    def refresh_processes(self) -> None:
        self.process_rows = process_snapshot()
        self.sort_cached_processes()

    def render_processes(self) -> None:
        table = self.process_table
        scroll_y = table.scroll_y

        query = self.process_filter.strip().lower()

        rows = self.process_rows

        if query:
            rows = [
                process
                for process in rows
                if query in process["name"].lower()
                or query in str(process["pid"])
            ]

        visible_by_pid = {
            str(process["pid"]): process
            for process in rows
        }

        desired_pids = set(visible_by_pid)

        # Remove processes that exited or no longer match the filter.
        for pid in self.rendered_pids - desired_pids:
            table.remove_row(pid)

        self.rendered_pids &= desired_pids

        # Add new rows and update existing rows.
        for process in rows:
            pid = str(process["pid"])

            gpu_text = (
                f"{process['gpu']:.0f}%"
                if process["gpu"] is not None
                else "—"
            )

            vram_text = (
                human_bytes(process["vram"])
                if process["vram"] > 0
                else "—"
            )

            values = {
                "pid": pid,
                "process": process["name"][:36],
                "cpu": f"{process['cpu']:.1f}%",
                "gpu": gpu_text,
                "ram": human_bytes(process["rss"]),
                "vram": vram_text,
            }

            if pid not in self.rendered_pids:
                table.add_row(
                    values["pid"],
                    values["process"],
                    values["cpu"],
                    values["gpu"],
                    values["ram"],
                    values["vram"],
                    key=pid,
                )

                self.rendered_pids.add(pid)

            else:
                self.update_process_cell(pid, "process", values["process"])
                self.update_process_cell(pid, "cpu", values["cpu"])
                self.update_process_cell(pid, "gpu", values["gpu"])
                self.update_process_cell(pid, "ram", values["ram"])
                self.update_process_cell(pid, "vram", values["vram"])

        # Reorder the table according to the current numeric snapshot.
        def table_sort_key(row_data):
            pid = str(row_data[0])
            process = visible_by_pid.get(pid)

            if process is None:
                return -1.0

            return self.process_sort_key(process)

        table.sort(
            key=table_sort_key,
            reverse=True,
        )

        labels = {
            "cpu": "CPU",
            "gpu": "GPU",
            "memory": "RAM",
            "vram": "VRAM",
        }

        label = labels[self.process_sort]

        filter_text = (
            f"  [dim]filter: {escape(self.process_filter)}[/]"
            if self.process_filter
            else ""
        )

        self.process_title_widget.update(
            f"[bold {ACCENT}]TOP PROCESSES[/]  "
            f"[dim]sorted by {label}[/]"
            f"{filter_text}  "
            f"[dim]({len(rows)})[/]"
        )

        table.scroll_to(
            y=scroll_y,
            animate=False,
        )

    def process_sort_key(self, process: dict) -> float:
        if self.process_sort == "memory":
            return float(process["rss"])

        if self.process_sort == "gpu":
            gpu = process["gpu"]
            return float(gpu) if gpu is not None else -1.0

        if self.process_sort == "vram":
            return float(process["vram"])

        return float(process["cpu"])

    def sort_cached_processes(self) -> None:
        self.process_rows.sort(
            key=self.process_sort_key,
            reverse=True,
        )

        self.render_processes()

    def action_sort_cpu(self) -> None:
        self.process_sort = "cpu"
        self.sort_cached_processes()

    def action_sort_gpu(self) -> None:
        self.process_sort = "gpu"
        self.sort_cached_processes()    

    def action_sort_memory(self) -> None:
        self.process_sort = "memory"
        self.sort_cached_processes()

    def action_sort_vram(self) -> None:
        self.process_sort = "vram"
        self.sort_cached_processes()

    def action_refresh(self) -> None:
        self.refresh_fast_metrics()
        self.refresh_gpu()
        self.refresh_disks()
        self.refresh_processes()

    def action_search_processes(self) -> None:
        search = self.process_search_widget

        search.display = True
        search.value = self.process_filter
        search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "process-search":
            return

        self.process_filter = event.value
        self.render_processes()
    
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "process-search":
            return

        event.input.display = False
        self.set_focus(None)

    def action_clear_search(self) -> None:
        search = self.process_search_widget

        if search.display:
            search.display = False
            self.set_focus(None)
            return

        if self.process_filter:
            self.process_filter = ""
            search.value = ""
            self.render_processes()

    def action_process_home(self) -> None:
        self.process_table.scroll_home(
            animate=False
        )


    def action_process_end(self) -> None:
        self.process_table.scroll_end(
            animate=False
        )


    def action_process_page_up(self) -> None:
        table = self.process_table
        table.scroll_relative(
            y=-max(table.size.height - 3, 1),
            animate=False,
        )


    def action_process_page_down(self) -> None:
        table = self.process_table
        table.scroll_relative(
            y=max(table.size.height - 3, 1),
            animate=False,
        )



def main() -> None:
    SystemMonitor().run()