// AlphaMind Neo4j constraints, indexes, and validation queries
// Run after Neo4j is reachable and after a small WeKnora graph extraction smoke test.
// If WeKnora writes nodes without the referenced properties, inspect first with:
//   MATCH (n) RETURN labels(n) AS labels, keys(n) AS keys, count(*) AS count ORDER BY count DESC LIMIT 50;

// ---------- Constraints / indexes ----------
CREATE CONSTRAINT alphamind_company_id IF NOT EXISTS
FOR (n:Company) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT alphamind_report_doc_id IF NOT EXISTS
FOR (n:Report) REQUIRE n.doc_id IS UNIQUE;

CREATE CONSTRAINT alphamind_broker_name IF NOT EXISTS
FOR (n:Broker) REQUIRE n.name IS UNIQUE;

CREATE INDEX alphamind_company_name IF NOT EXISTS
FOR (n:Company) ON (n.name);

CREATE INDEX alphamind_company_ticker IF NOT EXISTS
FOR (n:Company) ON (n.ticker);

CREATE INDEX alphamind_report_publish_date IF NOT EXISTS
FOR (n:Report) ON (n.publish_date);

CREATE INDEX alphamind_report_broker IF NOT EXISTS
FOR (n:Report) ON (n.broker);

CREATE INDEX alphamind_metric_name_period IF NOT EXISTS
FOR (n:FinancialMetric) ON (n.name, n.period);

CREATE INDEX alphamind_event_type_date IF NOT EXISTS
FOR (n:Event) ON (n.type, n.event_date);

// ---------- Smoke validation ----------
MATCH (n)
RETURN labels(n) AS labels, count(*) AS count
ORDER BY count DESC;

MATCH ()-[r]->()
RETURN type(r) AS relation, count(*) AS count
ORDER BY count DESC;

// Company coverage by report count.
MATCH (r:Report)-[:COVERS]->(c:Company)
RETURN c.name AS company, c.ticker AS ticker, count(r) AS report_count
ORDER BY report_count DESC
LIMIT 20;

// Financial metric extraction quality.
MATCH (r:Report)-[:HAS_METRIC]->(m:FinancialMetric)-[:ABOUT_COMPANY]->(c:Company)
RETURN c.name AS company, c.ticker AS ticker, m.name AS metric, m.period AS period,
       m.value AS value, m.unit AS unit, r.title AS report_title, r.publish_date AS publish_date
ORDER BY company, period
LIMIT 50;

// Investment view / rating / target price extraction.
MATCH (r:Report)-[:HAS_VIEW]->(v:InvestmentView)-[:ABOUT_COMPANY]->(c:Company)
RETURN c.name AS company, c.ticker AS ticker, r.broker AS broker, r.publish_date AS publish_date,
       v.rating AS rating, v.target_price AS target_price, v.thesis AS thesis
ORDER BY publish_date DESC
LIMIT 50;

// Risk extraction.
MATCH (r:Report)-[:HAS_RISK]->(risk:RiskFactor)
OPTIONAL MATCH (risk)-[:AFFECTS]->(c:Company)
RETURN coalesce(c.name, '') AS company, risk.name AS risk, risk.description AS description,
       r.title AS report_title, r.publish_date AS publish_date
ORDER BY publish_date DESC
LIMIT 50;

// Event extraction.
MATCH (e:Event)-[:AFFECTS]->(c:Company)
RETURN c.name AS company, c.ticker AS ticker, e.type AS event_type,
       e.event_date AS event_date, e.description AS description
ORDER BY event_date DESC
LIMIT 50;
