import json
import asyncio
from pathlib import Path
from typing import Dict, Any


class TaskManager:
    def __init__(self, status_file: Path):
        self.status_file = status_file
        self.lock = asyncio.Lock()
        self.tasks: Dict[str, Any] = {}

        # при старте загружаем статусы
        if self.status_file.exists():
            try:
                self.tasks = json.loads(self.status_file.read_text())
            except Exception:
                self.tasks = {}

    async def save(self):
        """Асинхронно сохраняет все статусы в файл"""
        async with self.lock:
            tmp_path = self.status_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2))
            tmp_path.replace(self.status_file)

    async def set_status(self, task_id: str, status: str, **extra):
        """Обновляет статус задачи и сохраняет"""
        async with self.lock:
            if task_id not in self.tasks:
                self.tasks[task_id] = {}
            self.tasks[task_id].update({"status": status, **extra})
        await self.save()

    def get(self, task_id: str) -> Dict[str, Any] | None:
        return self.tasks.get(task_id)