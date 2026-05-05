# crypto-trading-bot configuration
# Live test phase, $100 bankroll on Hyperliquid (Perps)

strategy:
  pairs:
    - "BTC/USDC:USDC"
    - "ETH/USDC:USDC"
    - "SOL/USDC:USDC"

  setup_a:
    setup_atr_extension: 1.0
    rsi_threshold: 40.0
    adx_max: 30.0
    funding_threshold: -0.00001

    limit_extension_atr: 2.5

    stop_atr: 1.5
    target_mode: vwap
    target_pct: 0.01

    limit_validity_hours: 8
    position_max_hours: 48

bankroll:
  initial_capital_usd: 100.0
  risk_per_trade_pct: 0.02
  max_concurrent_positions: 1
  max_leverage: 3.0

  daily_loss_limit_pct: 0.05
  max_drawdown_pct: 0.20

  sizing_model: fixed
  kelly_fraction: 0.25

frictions:
  exchange: hyperliquid
  # Real fees from your HL account (visible in Portfolio screen):
  maker_fee_bps: 1.44             # 0.0144% maker
  taker_fee_bps: 4.32             # 0.0432% taker

telegram:
  enabled: true
