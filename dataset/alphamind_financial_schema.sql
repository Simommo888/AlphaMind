-- AlphaMind financial structured-data schema (SQLite MVP)
-- Use for local PoC. For production, migrate the same logical schema to Postgres/DuckDB/ClickHouse.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
  company_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  full_name TEXT,
  ticker TEXT,
  exchange TEXT,
  aliases_json TEXT DEFAULT '[]',
  source TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS reports (
  doc_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_type TEXT,
  broker TEXT,
  publish_date TEXT,
  report_type TEXT,
  file_path TEXT,
  pages INTEGER,
  language TEXT DEFAULT 'zh-CN',
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS financial_metrics (
  metric_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  metric_name TEXT NOT NULL,
  period TEXT NOT NULL,
  value REAL,
  unit TEXT,
  currency TEXT,
  source_doc_id TEXT,
  source_chunk_id TEXT,
  page INTEGER,
  broker TEXT,
  analyst TEXT,
  confidence REAL,
  created_at TEXT,
  FOREIGN KEY(company_id) REFERENCES companies(company_id),
  FOREIGN KEY(source_doc_id) REFERENCES reports(doc_id)
);

CREATE INDEX IF NOT EXISTS idx_financial_metrics_company_period
ON financial_metrics(company_id, period);

CREATE INDEX IF NOT EXISTS idx_financial_metrics_name_period
ON financial_metrics(metric_name, period);

CREATE INDEX IF NOT EXISTS idx_financial_metrics_source_doc
ON financial_metrics(source_doc_id);

CREATE TABLE IF NOT EXISTS metric_lineage (
  lineage_id TEXT PRIMARY KEY,
  metric_id TEXT NOT NULL,
  raw_file_path TEXT,
  raw_sheet TEXT,
  raw_cell TEXT,
  raw_text TEXT,
  parser TEXT,
  imported_at TEXT,
  FOREIGN KEY(metric_id) REFERENCES financial_metrics(metric_id)
);

CREATE INDEX IF NOT EXISTS idx_metric_lineage_metric
ON metric_lineage(metric_id);
