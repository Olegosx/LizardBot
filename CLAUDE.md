# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**LizardBot** — торговый бот для [Polymarket](https://polymarket.com), работающий с BTC "Up or Down" рынками. Реализует оптимальный вариант **Гипотезы 1**: ставка на ведущий исход за 30 мин до закрытия рынка, только если `std_dev(probability) ≤ 0.20`.

Доступ: `https://127.0.0.1/LizardBot/`

## Tech Stack

- **Backend**: Python 3.x + FastAPI + SQLite
- **Frontend**: Bootstrap + JavaScript + Chart.js (WebSocket real-time)
- **Polymarket**: официальный `py-clob-client`

## Architecture

Подробная архитектура в **`ARCHITECTURE.md`** (C4, до уровня классов и методов).

### Три потока

| Поток | Класс | Задача |
|-------|-------|--------|
| Thread 1 | `BotEngine` | Логика бота, Polymarket API, торговля |
| Thread 2 | `APIServer` | HTTP + WebSocket для фронтенда |
| Thread 3 | `ConfigLoader` | Горячая перезагрузка `config.json` каждые N сек |

**Единственный канал связи между потоками — `SharedState`** (защищён `threading.RLock`). Перед началом каждой итерации поток делает `get_config_snapshot()` — локальную копию конфига, чтобы не поймать обновление посередине итерации.

### Ключевые правила разработки

- **Строго по `ARCHITECTURE.md`**: перед созданием любой новой сущности — сначала обновить схему
- При изменении любого компонента — синхронно обновить `ARCHITECTURE.md`
- Выход за рамки схемы без согласования запрещён

## Trading Strategy — LizardStrategy (Hypothesis 1, Optimal)

- **Смещение**: T−30 мин до закрытия рынка
- **Фильтр**: `std_dev(prob, last_30min) ≤ 0.20` — единственный фильтр, улучшающий ROI
- **Результат**: 474 сделки, Winrate 95.4%, ROI **+9.00%** (vs +5.58% без фильтра)
- **Опасная зона**: вероятность 0.80–0.90 — комиссия съедает прибыль; поведение задаётся `danger_zone_action` в конфиге

## Режимы работы

- `simulation_mode: true` — сделки эмулируются (без реальных ордеров), фронтенд показывает баннер **[SIMULATION]**
- `simulation_mode: false` — реальная торговля

## Configuration (`config.json`)

Файл в `.gitignore` (содержит `api_key`). Пример — `config.example.json`.

Ключевые параметры:
- `bet_mode`: `"fixed"` | `"percent"` | `"double_on_double"`
- `danger_zone_action`: `"skip"` | `"reduce"` | `"trade"`
- `recovery_action`: `"skip"` | `"enter"` | `"enter_if_safe"` (при рестарте после офлайна)
- `vol_threshold`: `0.20` (порог фильтра)
- `users`: `{username: bcrypt_hash}` для Basic Auth фронтенда

## Development Commands

```bash
# Установка зависимостей
pip install -r requirements.txt

# Запуск бота
python src/main.py

# Тесты
pytest

# Один тест
pytest tests/test_strategy.py::test_volatility_filter -v

# Линтер
flake8 src/ && mypy src/
```

## Project Structure

```
LizardBot/
├── src/
│   ├── main.py
│   ├── shared/          # SharedState + все dataclass-модели
│   ├── config/          # BotConfig, ConfigLoader (Thread 3)
│   ├── client/          # PolymarketClient
│   ├── bot/             # BotEngine, Scanner, Tracker, Strategy, OrderManager
│   ├── db/              # DBRepository (SQLite)
│   └── api/             # FastAPI, routes, WebSocket, BasicAuth
├── frontend/            # Bootstrap + Chart.js SPA
├── data/lizardbot.db    # SQLite (создаётся автоматически)
├── logs/lizardbot.log
├── config.json          # ! в .gitignore
├── config.example.json
└── ARCHITECTURE.md      # Полная C4-схема
```

## State Persistence & Recovery

При рестарте бот восстанавливает состояние из DB:
1. Загружает активные рынки и открытые позиции
2. Определяет, сколько времени был офлайн
3. Для рынков, у которых пропущено окно T−30: применяет `recovery_action` из конфига
4. Для закрытых рынков, у которых нет результата в DB: запрашивает результат с Polymarket и закрывает позицию
