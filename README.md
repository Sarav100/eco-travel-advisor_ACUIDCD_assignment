# 🌱 EcoTrip – Rasa Eco-Travel Advisor

Conversational agent that helps travellers plan low-carbon trips: adaptive trip
intake, Climatiq carbon estimates, Amadeus sandbox hotel data, weighted ranking,
two-stage fallback and context-rich human handover.

## Project structure
```
actions/        services.py (API + scoring logic), actions.py (Rasa wrappers)
data/           nlu.yml, rules.yml, stories.yml
tests/          test_stories.yml (rasa test core), test_services.py (pytest)
frontend/       index.html (React + Rasa REST channel)
mock_data/      eco_hotels.json (curated eco-certification data)
config.yml  domain.yml  endpoints.yml  credentials.yml  docker-compose.yml
```

## Replication (local, Python 3.10)
```bash
git clone https://github.com/<your-username>/eco-travel-advisor.git
cd eco-travel-advisor
python3.10 -m venv venv && source venv/bin/activate
pip install rasa==3.6.20 rasa-sdk==3.6.2 requests pytest
python -m spacy download en_core_web_md
cp .env.example .env            # add API keys (optional - fallbacks work without)
rasa train
rasa run actions --port 5055 &  # terminal 1
rasa run --enable-api --cors "*" # terminal 2
# open frontend/index.html in a browser
```
Set `action_endpoint` in endpoints.yml to `http://localhost:5055/webhook` for local runs.

## Testing
```bash
rasa test nlu --cross-validation --folds 5    # results/intent_report.json, confusion matrix
rasa test nlu --nlu data/nlu.yml --percentage 20   # 80/20 split
rasa test core --stories tests/test_stories.yml  # results/failed_test_stories.yml
pytest -v tests/test_services.py               # 15 unit tests, APIs mocked
```

## Deployment (Docker)
```bash
cp .env.example .env && docker compose up --build
# Frontend http://localhost:3000 · Rasa http://localhost:5005
```
HuggingFace Spaces: create a Docker Space, push this repo, add the keys under
*Settings → Variables and secrets*. For development tunnelling:
`from pyngrok import ngrok; print(ngrok.connect(5005))`.

## API keys
| Service | Free tier | Variable |
|---|---|---|
| Climatiq | Community plan | CLIMATIQ_API_KEY |
| Amadeus for Developers | Test sandbox | AMADEUS_CLIENT_ID / SECRET |
| OpenCage | Daily free limit | OPENCAGE_API_KEY |

## Privacy
No data persists after a session (`carry_over_slots_to_new_session: false`),
sender IDs are hashed before handover, and keys live only in `.env`.
