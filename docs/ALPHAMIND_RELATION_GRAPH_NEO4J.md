# AlphaMind Neo4j 板块—产业链—企业链查看手册

AlphaMind 已将本地企业资料构建为独立的 `AlphaMindEntity` 图层，不覆盖 WeKnora 自己抽取的动态图谱节点。

## 当前地址

- Neo4j Browser：`http://localhost:17474`
- Browser 连接地址：`neo4j://localhost:17687`（不要使用页面默认的 `localhost:7687`）
- 数据库：`neo4j`
- 用户名和密码：读取 `.env.phase3-precision` 中的 `NEO4J_USERNAME` / `NEO4J_PASSWORD`

## 图谱口径

节点：

- `Sector`：AlphaMind 主题板块。
- `Industry`：本地企业资料的目录行业。
- `ChainStage`：上游资源材料、中游制造、下游应用服务。
- `Company`：企业 Markdown 文件对应的企业主体。

关系：

- `Company-[:BELONGS_TO]->Industry`
- `Industry-[:PART_OF_SECTOR]->Sector`
- `Industry-[:PART_OF_CHAIN]->ChainStage`
- `ChainStage-[:UPSTREAM_OF]->ChainStage`
- `Company-[:SUPPLIES_TO]->Company`
- `Company-[:PARTNERS_WITH]->Company`
- `Company-[:COMPETES_WITH]->Company`
- `Company-[:CONTROLS]->Company`

分类关系来自本地目录和显式规则，`taxonomy_source` 会说明口径，不冒充申万/中信官方分类。企业间关系均保留：

- `source_file`
- `source_line`
- `source_doc_id`：`local-md:*` 本地 Markdown 稳定标识，不冒充 WeKnora 文档 ID。
- `source_chunk_id`：`local-line:*` 本地行证据稳定标识，不冒充 WeKnora chunk ID。
- `page`：Markdown 证据固定为 `0`。
- `evidence_quote`
- `evidence_method`
- `confidence`
- `evidence_json`

## 1. 查看整个板块的行业和企业

```cypher
MATCH p=(s:Sector {name: '新能源'})<-[:PART_OF_SECTOR]-(i:Industry)<-[:BELONGS_TO]-(c:Company)
RETURN p
LIMIT 150;
```

可替换板块：新能源、科技、汽车产业、高端制造、资源材料、消费、医药医疗、金融地产、公共事业、其他。

## 2. 查看板块的上中下游结构

```cypher
MATCH sectorPath=(s:Sector {name: '新能源'})<-[:PART_OF_SECTOR]-(i:Industry)-[:PART_OF_CHAIN]->(cs:ChainStage)
OPTIONAL MATCH chainPath=(cs)-[:UPSTREAM_OF]->(next:ChainStage)
RETURN sectorPath, chainPath
LIMIT 150;
```

## 3. 查看某家企业所属板块、行业和产业链环节

```cypher
MATCH p=(c:Company {name: '奥特维'})-[:BELONGS_TO]->(i:Industry)-[:PART_OF_SECTOR]->(s:Sector)
MATCH q=(i)-[:PART_OF_CHAIN]->(cs:ChainStage)
RETURN p, q;
```

## 4. 展开某家企业的客户、供应、合作、竞争和控制网络

```cypher
MATCH p=(c:Company {name: '奥特维'})-[:SUPPLIES_TO|PARTNERS_WITH|COMPETES_WITH|CONTROLS*1..2]-(peer:Company)
RETURN p
LIMIT 100;
```

## 5. 同时查看企业分类路径和企业关系

```cypher
MATCH taxonomy=(c:Company {name: '奥特维'})-[:BELONGS_TO]->(i:Industry)-[:PART_OF_SECTOR]->(s:Sector)
MATCH chain=(i)-[:PART_OF_CHAIN]->(cs:ChainStage)
OPTIONAL MATCH business=(c)-[:SUPPLIES_TO|PARTNERS_WITH|COMPETES_WITH|CONTROLS]-(peer:Company)
RETURN taxonomy, chain, business
LIMIT 100;
```

## 6. 查看一条关系的原文证据

```cypher
MATCH (c:Company {name: '奥特维'})-[r]-(peer:Company)
RETURN
  c.name AS company,
  type(r) AS relation,
  peer.name AS related_company,
  r.confidence AS confidence,
  r.source_file AS source_file,
  r.source_line AS source_line,
  r.evidence_quote AS evidence
ORDER BY relation, related_company;
```

## 7. 查找两个企业之间的最短关系链

```cypher
MATCH (a:Company {name: '奥特维'}), (b:Company {name: '晶科能源'})
MATCH p=shortestPath((a)-[:SUPPLIES_TO|PARTNERS_WITH|COMPETES_WITH|CONTROLS*..5]-(b))
RETURN p;
```

## 8. 查看规模和关系类型

```cypher
MATCH (n:AlphaMindEntity)
RETURN n.entity_type AS type, count(*) AS count
ORDER BY type;
```

```cypher
MATCH (:AlphaMindEntity)-[r]->(:AlphaMindEntity)
RETURN type(r) AS relation, count(*) AS count
ORDER BY relation;
```

## 重建和重新导入

构建 JSON：

```bash
python scripts/alphamind_relation_graph.py \
  --source-root 'D:/AlphaMind/数据/0710/企业md' \
  --output dataset/alphamind_relation_graph.json \
  --report dataset/alphamind_relation_graph_report.json \
  --import-neo4j \
  --env-file .env.phase3-precision
```

导入器只替换带 `AlphaMindEntity` 标签的节点，不删除 WeKnora 的其他 Neo4j 数据；导入语句采用参数化 Cypher，并创建唯一约束和名称/类型索引。
