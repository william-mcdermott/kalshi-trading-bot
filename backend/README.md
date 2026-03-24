# Polymarket Bot — Backend

A Python/FastAPI backend for running algorithmic trading bots on Polymarket.
Built as a portfolio project to demonstrate Python, REST APIs, and data pipelines.

---

## Stack

| Layer        | Tool          | JS equivalent       |
|--------------|---------------|---------------------|
| Web framework| FastAPI       | Express             |
| Server       | Uvicorn       | nodemon             |
| Database     | SQLite        | MongoDB (local)     |
| ORM          | SQLAlchemy    | Mongoose            |
| Validation   | Pydantic      | Zod / Joi           |
| HTTP client  | httpx         | axios               |
| Data         | pandas        | —                   |
| Indicators   | pandas-ta     | —                   |
| Testing      | pytest        | Jest                |

---

## Setup

```bash
# 1. Create a virtual environment (like node_modules but for Python)
python -m venv venv

# 2. Activate it
source venv/bin/activate      # Mac/Linux
venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill in environment variables
cp .env.example .env
# Edit .env with your Polymarket private key and wallet address

# 5. Create the data directory
mkdir -p data

# 6. Seed fake data (for frontend development without real trades)
python scripts/seed_fake_data.py

# 7. Start the server
uvicorn main:app --reload
```

The server runs at http://localhost:8000

---

## API Endpoints

| Method | Path                        | Description                    |
|--------|-----------------------------|--------------------------------|
| GET    | /health                     | Health check                   |
| GET    | /api/trades                 | All trades (supports ?strategy=macd) |
| GET    | /api/trades/summary         | P&L summary across strategies  |
| GET    | /api/bots                   | Status of all bots             |
| POST   | /api/bots/start             | Start a bot                    |
| POST   | /api/bots/stop/{strategy}   | Stop a bot                     |
| GET    | /api/market/events          | Active Polymarket markets      |
| GET    | /api/market/markets/{id}    | Single market details          |

Interactive docs (like Swagger): http://localhost:8000/docs

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Project Structure

```
polymarket-bot/
├── main.py                  # App entry point
├── requirements.txt
├── .env.example
├── app/
│   ├── routes/
│   │   ├── trades.py        # Trade history endpoints
│   │   ├── bots.py          # Bot control endpoints
│   │   └── market.py        # Polymarket data endpoints
│   ├── models/
│   │   ├── database.py      # SQLAlchemy table definitions
│   │   └── db.py            # DB connection + session management
│   └── bots/
│       └── macd_strategy.py # MACD signal generation
├── scripts/
│   └── seed_fake_data.py    # Populates DB with fake trades
├── tests/
│   └── test_macd_strategy.py
└── data/                    # SQLite database lives here (git-ignored)
```

---

## Next Steps

1. Build the Angular/React dashboard against `/api/trades` and `/api/bots`
2. Add RSI and CVD strategies in `app/bots/`
3. Wire up real Polymarket trading in a `app/services/trader.py`
4. Add background task runner so bots execute on a schedule
