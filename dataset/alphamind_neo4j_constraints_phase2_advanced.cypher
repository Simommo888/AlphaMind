// AlphaMind phase2-advanced Neo4j constraints and smoke queries.
// Run after Neo4j starts and before/after small-batch ingestion as needed.

CREATE CONSTRAINT alphamind_phase2_company_id IF NOT EXISTS
FOR (n:Company) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT alphamind_phase2_report_doc_id IF NOT EXISTS
FOR (n:Report) REQUIRE n.doc_id IS UNIQUE;

CREATE INDEX alphamind_phase2_company_name IF NOT EXISTS
FOR (n:Company) ON (n.name);

CREATE INDEX alphamind_phase2_company_ticker IF NOT EXISTS
FOR (n:Company) ON (n.ticker);

CREATE INDEX alphamind_phase2_report_broker IF NOT EXISTS
FOR (n:Report) ON (n.broker);

CREATE INDEX alphamind_phase2_report_publish_date IF NOT EXISTS
FOR (n:Report) ON (n.publish_date);

CREATE INDEX alphamind_phase2_financial_metric_name IF NOT EXISTS
FOR (n:FinancialMetric) ON (n.name);

CREATE INDEX alphamind_phase2_financial_metric_period IF NOT EXISTS
FOR (n:FinancialMetric) ON (n.period);

// Smoke validation queries:
// MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC LIMIT 20;
// MATCH ()-[r]->() RETURN type(r) AS relation, count(*) AS count ORDER BY count DESC LIMIT 20;
// MATCH (r:Report)-[:COVERS]->(c:Company) RETURN r.title, r.broker, r.publish_date, c.name, c.ticker LIMIT 20;
// MATCH (r:Report)-[:PUBLISHED_BY]->(b:Broker) RETURN r.title, b.name LIMIT 20;
// MATCH (r:Report)-[:HAS_METRIC]->(m:FinancialMetric) RETURN r.title, m.name, m.period, m.value, m.unit LIMIT 20;
