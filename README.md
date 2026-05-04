# crypto-trading-bot

Pro-grade crypto signal bot. **GitHub-native** : aucune exécution locale, tout tourne sur GitHub Actions.

- 🎯 **Setup A v2** — Mean reversion sur funding × extension × RSI (long & short symétriques)
- 📋 **Limit-order resting** — le bot émet des ordres limites prêts à placer, fire-and-forget
- 💰 **Gestion de bankroll pro** — sizing risk-based, circuit breakers (daily loss + max DD)
- 📲 **Telegram** — signaux + sizing + bankroll en temps réel
- 🧮 **Backtest reproductible** — workflow_dispatch avec params, artifacts uploadés
- 🔒 **State persisté en Gist** — auditable, séparé du repo

---

## Architecture

```
                ┌──────────────────────────────────────┐
                │  GitHub Actions                      │
                │                                      │
   cron 4h ──▶  │  scan.yml                            │
                │   ├─ pull OHLCV + funding (Binance)  │
                │   ├─ détecte Setup A v2              │
                │   ├─ vérifie bankroll & circuit      │
                │   ├─ size la position                │
                │   └─ push Telegram                   │
                │                                      │
   manual ───▶  │  backtest.yml (params via UI)        │
                │   └─ artifacts CSV + equity curve    │
                │                                      │
   manual ───▶  │  bankroll_reset.yml                  │
                │   └─ reset state après halt          │
                └──────────────┬───────────────────────┘
                               │
                ┌──────────────┴───────────────────────┐
                │  GitHub Gist (state)                 │
                │   ├─ bankroll.json                   │
                │   ├─ emitted_signals.json            │
                │   ├─ signals_log.json                │
                │   └─ audit_log.json                  │
                └──────────────┬───────────────────────┘
                               │
                ┌──────────────┴───────────────────────┐
                │  Telegram bot (read-only output)     │
                │   📩 Signal détecté → ordres limites │
                │   ⚠️  Signal rejeté → raison         │
                │   🛑 Halt automatique                │
                └──────────────────────────────────────┘
```

---

## Setup en 5 étapes

### 1. Fork ce repo
Push tout le contenu vers un repo **privé** sur ton GitHub.

### 2. Créer un bot Telegram

- Ouvre [@BotFather](https://t.me/BotFather) sur Telegram
- `/newbot` → donne nom + username → tu obtiens un **token** (`123456:ABC...`)
- Lance ton bot, envoie-lui n'importe quel message
- Dans ton navigateur : `https://api.telegram.org/bot<TOKEN>/getUpdates`
- Récupère `chat.id` dans la réponse JSON

### 3. Créer un Gist pour le state

- Va sur https://gist.github.com
- Crée un Gist **secret** avec un fichier dummy (ex. `bankroll.json` contenant `{}`)
- Note l'ID du Gist (dans l'URL : `gist.github.com/username/<ID>`)

### 4. Créer un Personal Access Token (PAT)

- https://github.com/settings/tokens → Generate new token (classic)
- Scope : `gist` uniquement (sécurité minimale)
- Note le token

### 5. Configurer les Secrets GitHub

Dans ton repo → Settings → Secrets and variables → Actions → New repository secret :

| Nom | Valeur |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token Telegram (étape 2) |
| `TELEGRAM_CHAT_ID` | Chat ID (étape 2) |
| `GIST_PAT` | PAT GitHub (étape 4) |
| `GIST_STATE_ID` | ID du Gist (étape 3) |

### 6. Initialiser la bankroll

Dans le repo → Actions → `Reset bankroll state` → **Run workflow** :
- `new_equity` = ton capital de départ (ex. `10000`)
- `reason` = `initial setup`

C'est fait. Le scan démarrera automatiquement aux prochains 00:01, 04:01, 08:01... UTC.

---

## Workflows disponibles

| Workflow | Trigger | Description |
|---|---|---|
| `scan.yml` | cron 4h + manuel | Détecte Setup A v2, push Telegram |
| `backtest.yml` | manuel uniquement | Backtest paramétrable, artifacts |
| `bankroll_reset.yml` | manuel uniquement | Reset state (après halt ou changement capital) |

---

## Configuration (config.yaml)

Tous les paramètres sont externalisés. Édite `config.yaml` puis `git push`. Le scan suivant utilisera les nouvelles valeurs.

**Bankroll critique :**
- `risk_per_trade_pct: 0.01` → 1% de l'equity à risque par trade
- `max_concurrent_positions: 3` → max 3 positions ouvertes simultanées
- `max_leverage: 3.0` → cap de levier implicite (sécurité)
- `daily_loss_limit_pct: 0.03` → -3% en 24h → halt automatique 24h
- `max_drawdown_pct: 0.15` → -15% du peak → halt jusqu'à reset manuel

**Setup A v2 :**
- `setup_atr_extension: 1.5` → seuil de détection
- `limit_extension_atr: 2.5` → où tu places la limite (plus profond → meilleur entry, fewer fills)
- `target_pct: 0.01` → +1% target

---

## Workflow de production recommandé

1. **Backtest exhaustif** sur 2023-2025 avec params par défaut. Vérifie Sharpe > 1, DD < 15%.
2. **Walk-forward** : refais le backtest sur 2025 seul. Si l'edge tient, deploy.
3. **Active scan.yml en mode dry-run** : commente la ligne `send_message` dans `scanner.py` pour 2 semaines, log dans Gist seulement. Compare aux backtest.
4. **Active Telegram**. Place les ordres manuellement.
5. **Reporte les résultats** : après chaque trade fermé, tu trigger `bankroll_reset.yml` avec ta nouvelle equity réelle.

---

## Sécurité / discipline

- ✅ **Idempotency** : un même signal ne sera jamais émis deux fois (signal_id en Gist)
- ✅ **Audit trail complet** : tous les signaux (émis + rejetés) dans `signals_log.json`
- ✅ **Circuit breakers automatiques** : daily loss + max DD → halt sans intervention
- ✅ **Anti-Martingale** : sizing toujours basé sur l'equity courant, jamais sur la perte précédente
- ✅ **Concurrency lock** : un seul scan tourne à la fois
- ✅ **Aucune exécution automatique** : le bot suggère, **tu** places les ordres → zero risk de bug d'exécution

---

## Limites & disclaimers

- Le bot suggère, n'exécute pas. Tu places les ordres manuellement sur Binance/Bybit.
- Le bankroll tracking suppose que tu reportes les résultats fidèlement via `bankroll_reset.yml`.
- GitHub Actions cron a 5-15 min de latence. Pour des setups 4h c'est OK, pour des 5min c'est rédhibitoire.
- Funding rate API limites Binance : ~30j d'historique par requête, le backtest paginate automatiquement.
- Pas de modélisation de slippage book-aware. Pour des positions <$50k sur BTC/ETH/SOL, négligeable.

---

## Stack

- Python 3.11
- `ccxt` — exchange API
- `pandas` / `numpy` — data + indicators
- `requests` — Telegram + Gist
- `pyyaml` — config
- `matplotlib` — backtest charts

---

## Licence

MIT. Use at own risk.
