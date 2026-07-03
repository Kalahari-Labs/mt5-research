-- 0001_init.sql — market-intel core schema (Postgres / Supabase)
-- Reproducible from a fresh Supabase project:
--   supabase db push   (or paste into the SQL editor / apply via MCP)
-- Every table carries: ts (when observed), symbol (what), source (where from).

create table if not exists price_snapshots (
    id          bigint generated always as identity primary key,
    ts          timestamptz not null default now(),
    symbol      text        not null,
    source      text        not null,           -- 'mt5_csv' | 'mt5_bridge' | 'coingecko' | ...
    timeframe   text        not null,           -- 'tick' | 'M15' | 'H1' | 'H4' | 'D1'
    bar_time    timestamptz not null,           -- open time of the bar this row describes
    open        double precision not null,
    high        double precision not null,
    low         double precision not null,
    close       double precision not null,
    volume      double precision not null default 0,
    unique (symbol, timeframe, bar_time, source)
);
create index if not exists idx_price_symbol_tf_time on price_snapshots (symbol, timeframe, bar_time desc);

create table if not exists technical_signals (
    id           bigint generated always as identity primary key,
    ts           timestamptz not null default now(),
    symbol       text        not null,
    source       text        not null,          -- 'intel.analysis.ta'
    timeframe    text        not null,
    -- DESCRIPTIVE layer: what the market is doing. No buy/sell fields by design.
    trend        text        not null,          -- 'up' | 'down' | 'flat'
    trend_strength   double precision,          -- 0..1 (|fast-slow| EMA gap, normalized by ATR)
    volatility_regime text       not null,      -- 'low' | 'normal' | 'high'
    atr          double precision,
    rsi          double precision,
    support      double precision,              -- nearest key level below close
    resistance   double precision,              -- nearest key level above close
    sweep        text,                          -- null | 'high_sweep' | 'low_sweep' (equal H/L swept then reversed)
    sweep_level  double precision,
    close        double precision not null,
    details      jsonb                          -- structured extras (level arrays etc.)
);
create index if not exists idx_signals_symbol_ts on technical_signals (symbol, ts desc);

create table if not exists news_events (
    id        bigint generated always as identity primary key,
    ts        timestamptz not null default now(),
    symbol    text        not null default 'MARKET',  -- symbol it maps to, or 'MARKET' for macro
    source    text        not null,                   -- feed name / domain
    published timestamptz,
    title     text        not null,
    url       text,
    summary   text,
    unique (source, title)
);
create index if not exists idx_news_ts on news_events (ts desc);

create table if not exists sentiment_scores (
    id        bigint generated always as identity primary key,
    ts        timestamptz not null default now(),
    symbol    text        not null,
    source    text        not null,           -- 'intel.analysis.sentiment'
    score     double precision not null,      -- -1 (bearish words) .. +1 (bullish words)
    n_items   integer     not null,           -- headlines scored in this window
    method    text        not null default 'keyword_rule_v1',
    details   jsonb
);
create index if not exists idx_sentiment_symbol_ts on sentiment_scores (symbol, ts desc);

create table if not exists market_state (
    id          bigint generated always as identity primary key,
    ts          timestamptz not null default now(),
    symbol      text        not null,
    source      text        not null,          -- 'intel.analysis.synthesis'
    trend       text,
    volatility_regime text,
    last_close  double precision,
    support     double precision,
    resistance  double precision,
    sweep       text,
    sentiment   double precision,              -- latest rolling sentiment for the symbol
    news_count_24h integer,
    -- Descriptive "trading point" context: levels the analysis identifies, with an
    -- empirical hit-rate. These describe structure; nothing reads them to trade.
    setup_bias      text,                      -- 'long-structure' | 'short-structure' | 'neutral'
    setup_entry     double precision,          -- level where structure suggests interest
    setup_target    double precision,          -- next opposing level
    setup_stop      double precision,          -- level that invalidates the structure
    setup_confidence double precision,         -- 0..1 empirical (see analysis/hitrate)
    summary     text,                          -- one-line human-readable state
    details     jsonb
);
create index if not exists idx_state_symbol_ts on market_state (symbol, ts desc);

create table if not exists system_health (
    id        bigint generated always as identity primary key,
    ts        timestamptz not null default now(),
    symbol    text        not null default 'SYSTEM',
    source    text        not null,            -- component name, e.g. 'collector.prices'
    status    text        not null,            -- 'ok' | 'degraded' | 'error'
    cycle     integer,
    latency_ms double precision,
    message   text
);
create index if not exists idx_health_source_ts on system_health (source, ts desc);
