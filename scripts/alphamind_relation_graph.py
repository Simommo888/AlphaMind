#!/usr/bin/env python3
"""Build an evidence-bearing AlphaMind sector/supply-chain/company graph.

The builder is deterministic: directory taxonomy creates sector/industry/company
edges and explicit sentences in company Markdown create inter-company edges.
Unknown or anonymous counterparties are never invented.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

SCHEMA_VERSION = "alphamind.relation_graph.v1"
CHAIN_STAGES = ("上游资源材料", "中游制造", "下游应用服务")

SECTOR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("新能源", ("光伏", "锂电", "电池", "储能", "风电", "氢能", "充电桩", "新能源")),
    ("科技", ("半导体", "芯片", "电子", "通信", "光学", "计算机", "软件", "IT服务", "人工智能", "元件", "PCB", "显示")),
    ("汽车产业", ("汽车", "车零部件", "乘用车", "商用车")),
    ("高端制造", ("设备", "机械", "自动化", "机器人", "工业", "仪器", "电机", "专用工程")),
    ("资源材料", ("金属", "铜", "铝", "钨", "钴", "稀土", "钢铁", "煤", "化工", "化学", "材料", "玻璃", "水泥", "造纸", "矿", "橡胶", "塑料")),
    ("消费", ("食品", "饮料", "白酒", "家电", "服装", "零售", "旅游", "传媒", "美容", "农业", "种植", "养殖")),
    ("医药医疗", ("医药", "医疗", "生物", "制药", "中药", "器械")),
    ("金融地产", ("银行", "保险", "证券", "多元金融", "房地产")),
    ("公共事业", ("电力", "环保", "公用", "交通", "建筑", "环境治理")),
)

UPSTREAM_KEYWORDS = (
    "原料", "材料", "金属", "铜", "铝", "钨", "钴", "锂", "稀土", "钢铁", "煤", "化工", "化学", "玻璃", "水泥", "矿", "橡胶", "塑料", "辅材", "硅料", "浆料",
)
MIDSTREAM_KEYWORDS = (
    "设备", "制造", "加工", "零部件", "组件", "半导体", "芯片", "电池", "电子", "机械", "自动化", "元件", "PCB", "电机", "仪器",
)
DOWNSTREAM_KEYWORDS = (
    "应用", "服务", "运营", "发电", "汽车", "家电", "软件", "传媒", "零售", "医疗", "食品", "饮料", "旅游", "银行", "保险", "地产", "交通",
)

ANONYMOUS_RE = re.compile(r"^(?:客户|供应商|单位|公司)[A-ZＡ-Ｚ一二三四五六七八九十0-9]+$")
AMBIGUOUS_COMPANY_NAMES = {"新产业", "动力源"}
TICKER_SUFFIX_RE = re.compile(r"[_（(]?(?:[0368]\d{5})(?:\.(?:SZ|SH|BJ))?[）)]?$", re.I)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")


def classify_sector(industry: str) -> str:
    name = str(industry or "").strip()
    for sector, keywords in SECTOR_RULES:
        if any(keyword.lower() in name.lower() for keyword in keywords):
            return sector
    return "其他"


def classify_chain_stage(industry: str) -> str:
    name = str(industry or "").strip()
    # Materials must win over equipment when an industry name contains both.
    if any(keyword.lower() in name.lower() for keyword in UPSTREAM_KEYWORDS):
        return "上游资源材料"
    if any(keyword.lower() in name.lower() for keyword in MIDSTREAM_KEYWORDS):
        return "中游制造"
    if any(keyword.lower() in name.lower() for keyword in DOWNSTREAM_KEYWORDS):
        return "下游应用服务"
    return "中游制造"


def normalize_company_name(stem: str) -> str:
    name = str(stem).strip()
    name = re.sub(r"\.phase3$", "", name, flags=re.I)
    name = re.sub(r"_(?:202[0-9]年报|企业研究报告)$", "", name)
    name = TICKER_SUFFIX_RE.sub("", name).strip(" _-（）()")
    return name


def _safe_id(prefix: str, name: str) -> str:
    return f"{prefix}:{name.strip()}"


def _edge_id(source: str, relation_type: str, target: str) -> str:
    raw = f"{source}\0{relation_type}\0{target}".encode("utf-8")
    return "edge:" + hashlib.sha1(raw).hexdigest()[:20]


def _local_evidence_ids(path: Path, line: int) -> dict[str, Any]:
    normalized = path.resolve().as_posix()
    doc_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]
    chunk_hash = hashlib.sha1(f"{normalized}:{line}".encode("utf-8")).hexdigest()[:20]
    return {
        "source_doc_id": f"local-md:{doc_hash}",
        "source_chunk_id": f"local-line:{chunk_hash}",
        "page": 0,
    }


def _evidence_excerpt(line: str, target_position: int, max_chars: int = 600) -> str:
    if len(line) <= max_chars:
        return line
    start = max(0, target_position - max_chars // 2)
    end = min(len(line), start + max_chars)
    start = max(0, end - max_chars)
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")


def _directory_evidence(path: Path, industry: str) -> dict[str, Any]:
    return {
        "source_file": path.as_posix(),
        "line": 0,
        "quote": f"目录分类：{industry}",
        "method": "directory_taxonomy",
        "taxonomy_source": "AlphaMind本地行业目录",
        **_local_evidence_ids(path, 0),
    }


def _industry_for(path: Path, roots: list[Path]) -> str:
    parent = path.parent.name.strip()
    conversion_markers = ("转换md", "企业md", "转md", "input")
    if not parent or any(marker.lower() in parent.lower() for marker in conversion_markers):
        return "未分类"
    return parent


def _nearest_keyword_distance(line: str, position: int, keywords: Iterable[str]) -> int | None:
    distances = [abs(line.find(keyword) - position) for keyword in keywords if keyword in line]
    return min(distances) if distances else None


def _target_context(line: str, target_position: int) -> tuple[str, int]:
    """Return the numbered sentence containing the target and its local offset."""
    boundaries = [0]
    boundaries.extend(match.end() for match in re.finditer(r"[。；;！？!?]|(?<!\d)\d+[）)]", line))
    boundaries.append(len(line))
    start = max(boundary for boundary in boundaries if boundary <= target_position)
    end = min(boundary for boundary in boundaries if boundary > target_position)
    return line[start:end].strip(), target_position - start


def _has_source_reference(text: str, source_company: str) -> bool:
    if source_company in text:
        return True
    return bool(
        re.search(r"(?:^|[，。；;：:\s是为向与和由及])(?:本公司|公司|我们)", text)
        or re.search(r"公司(?=设备|产品|服务|商品|材料|技术|股份|股权|业务)", text)
    )


def _resolve_relation(
    line: str,
    heading: str,
    source_company: str,
    target_company: str,
    target_position: int,
) -> tuple[str, str, float, str] | None:
    """Resolve one counterparty from explicit wording local to its mention."""
    context, local_position = _target_context(line, target_position)
    if not _has_source_reference(context, source_company):
        return None

    escaped_target = re.escape(target_company)
    relation_words = r"客户|供应商|供货|供应|采购|合作|竞争对手|竞品"
    if re.search(rf"(?:不存在|并非|不是|未形成|没有|无)(?:[^。；]{{0,20}})(?:{relation_words})|(?:{relation_words})(?:[^。；]{{0,20}})(?:不存在|并非|不是|未形成|没有|无)", context):
        return None

    target_controls_source = (
        re.search(rf"(?:公司|本公司)控股股东(?:为|是)[^。；，,]{{0,20}}{escaped_target}", context)
        or re.search(rf"实际控制人(?:为|是)[^。；]{{0,30}}{escaped_target}", context)
        or re.search(rf"{escaped_target}[^。；]{{0,24}}(?:为|是)公司[^。；]{{0,12}}(?:控股股东|实际控制人|实控人)", context)
    )
    if target_controls_source:
        return "CONTROLS", "explicit_controlling_shareholder", 0.97, "target_to_source"
    majority = r"(?<![\d.])(?:5[0-9](?:\.\d+)?|[6-9][0-9](?:\.\d+)?|100(?:\.0+)?)%"
    source_controls_target = (
        re.search(rf"公司[^。；]{{0,40}}(?:收购|取得)[^。；]{{0,40}}{escaped_target}[^。；]{{0,30}}(?:控制权|控股|{majority})", context)
        or re.search(rf"公司[^。；]{{0,30}}持有[^。；]{{0,30}}{escaped_target}[^。；]{{0,20}}{majority}", context)
        or re.search(rf"公司(?:的)?(?:全资|控股)?子公司[\s“”\"'（）()]*{escaped_target}", context)
        or re.search(rf"{escaped_target}[^。；]{{0,12}}(?:为|是)公司(?:的)?(?:全资|控股)?子公司", context)
    )
    if source_controls_target:
        return "CONTROLS", "explicit_control", 0.94, "source_to_target"

    source_is_supplier = (
        re.search(rf"公司(?:(?!供应商)[^。；]){{0,40}}(?:是|为|成为|作为)[^。；]{{0,40}}{escaped_target}[^。；]{{0,30}}供应商", context)
        or re.search(rf"公司[^。；]{{0,25}}(?:向|为)[^。；]{{0,12}}{escaped_target}[^。；]{{0,25}}(?:供货|供应(?!商)|交付)", context)
        or re.search(rf"{escaped_target}[^。；]{{0,30}}(?:采购|购买)[^。；]{{0,20}}公司", context)
    )
    if source_is_supplier:
        return "SUPPLIES_TO", "explicit_supplier_to_target", 0.96, "source_to_target"

    target_is_supplier = (
        re.search(rf"{escaped_target}[^。；]{{0,35}}(?:是|为|作为|属于)?[^。；]{{0,25}}供应商", context)
        or re.search(rf"供应商[^。；]{{0,30}}(?:包括|为|有|主要以)?[^。；]{{0,50}}{escaped_target}", context)
        or re.search(rf"公司[^。；]{{0,20}}(?:向|从|由)[^。；]{{0,12}}{escaped_target}[^。；]{{0,20}}(?:采购|购买)", context)
    )
    if target_is_supplier:
        return "SUPPLIES_TO", "explicit_supplier", 0.96, "target_to_source"

    competition = (
        re.search(rf"(?:竞争对手|竞品)(?:主要)?(?:包括|有|为|是|：|:)[^。；]{{0,80}}{escaped_target}", context)
        or re.search(rf"公司[^。；]{{0,20}}(?:将|把)[^。；]{{0,10}}{escaped_target}[^。；]{{0,25}}(?:列为|视为)[^。；]{{0,10}}(?:竞争对手|竞品)", context)
        or re.search(rf"{escaped_target}[^。；]{{0,25}}(?:是|为|作为)?[^。；]{{0,20}}(?:竞争对手|竞品)", context)
    )
    if competition:
        return "COMPETES_WITH", "explicit_competition", 0.90, "source_to_target"

    if any(key in heading for key in ("供应商", "采购")):
        return "SUPPLIES_TO", "heading_supplier", 0.82, "target_to_source"
    if any(key in heading for key in ("客户", "销售", "订单")):
        return "SUPPLIES_TO", "heading_customer", 0.80, "source_to_target"

    partner_terms = r"共同参与|共同研发|共同开发|战略合作|合作关系|联合开发|合作伙伴|展开合作|深度合作|达成合作"
    partnership = (
        re.search(rf"公司[^。；]{{0,18}}(?:与|和|同|跟)[^。；]{{0,8}}{escaped_target}[^。；]{{0,30}}(?:{partner_terms})", context)
        or re.search(rf"{escaped_target}[^。；]{{0,18}}(?:与|和|同|跟)[^。；]{{0,8}}公司[^。；]{{0,30}}(?:{partner_terms})", context)
        or re.search(rf"(?:{partner_terms})[^。；]{{0,35}}{escaped_target}", context)
    )
    if partnership:
        return "PARTNERS_WITH", "explicit_partnership", 0.90, "source_to_target"

    customer = (
        re.search(rf"{escaped_target}[^。；]{{0,30}}(?:是|为|属于)?[^。；]{{0,20}}公司(?:的)?(?:主要|核心|重要)?客户", context)
        or re.search(rf"(?:客户|下游客户)[^。；]{{0,30}}(?:包括|为|有|主要包括|主要有)[^。；]{{0,60}}{escaped_target}", context)
        or re.search(rf"公司[^。；]{{0,25}}(?:向|为)[^。；]{{0,10}}{escaped_target}[^。；]{{0,20}}(?:销售|供货|供应|交付)", context)
    )
    if customer:
        return "SUPPLIES_TO", "explicit_customer", 0.94, "source_to_target"

    if any(key in heading for key in ("竞争对手", "竞争格局", "竞品")):
        return "COMPETES_WITH", "heading_competition", 0.82, "source_to_target"
    return None


def _compile_company_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    aliases = sorted(
        {
            name
            for name in names
            if len(name.strip()) >= 3
            and name.strip() not in AMBIGUOUS_COMPANY_NAMES
            and not ANONYMOUS_RE.match(name.strip())
        },
        key=lambda item: (-len(item), item),
    )
    if not aliases:
        return None
    return re.compile("|".join(re.escape(alias) for alias in aliases), re.I)


def build_graph(source_roots: Iterable[Path | str]) -> dict[str, Any]:
    roots = [Path(root).resolve() for root in source_roots]
    markdown_files = sorted({path.resolve() for root in roots for path in root.rglob("*.md") if path.is_file()}, key=lambda p: p.as_posix())

    docs: list[dict[str, Any]] = []
    company_sources: dict[str, list[Path]] = defaultdict(list)
    company_industries: dict[str, set[str]] = defaultdict(set)
    for path in markdown_files:
        company = normalize_company_name(path.stem)
        if not company or ANONYMOUS_RE.match(company):
            continue
        industry = _industry_for(path, roots)
        docs.append({"path": path, "company": company, "industry": industry})
        company_sources[company].append(path)
        company_industries[company].add(industry)

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_node(node_id: str, node_type: str, name: str, **attrs: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type, "name": name, **attrs}
        else:
            for key, value in attrs.items():
                if value not in (None, "", []):
                    nodes[node_id].setdefault(key, value)

    def add_edge(source: str, relation_type: str, target: str, evidence: dict[str, Any], *, confidence: float, label: str = "") -> None:
        key = (source, relation_type, target)
        edge = edges.get(key)
        if edge is None:
            edge = {
                "id": _edge_id(*key),
                "source": source,
                "target": target,
                "type": relation_type,
                "label": label or relation_type,
                "confidence": confidence,
                "evidence": [],
            }
            edges[key] = edge
        edge["confidence"] = max(float(edge["confidence"]), confidence)
        fingerprint = (evidence.get("source_file"), evidence.get("line"), evidence.get("quote"), evidence.get("method"))
        seen = {(item.get("source_file"), item.get("line"), item.get("quote"), item.get("method")) for item in edge["evidence"]}
        if fingerprint not in seen:
            edge["evidence"].append(evidence)

    industries = sorted({doc["industry"] for doc in docs})
    for industry in industries:
        sector = classify_sector(industry)
        stage = classify_chain_stage(industry)
        sector_id = _safe_id("sector", sector)
        industry_id = _safe_id("industry", industry)
        stage_id = _safe_id("stage", f"{sector}:{stage}")
        add_node(sector_id, "Sector", sector, taxonomy_source="AlphaMind本地行业目录")
        add_node(industry_id, "Industry", industry, sector=sector, chain_stage=stage, taxonomy_source="AlphaMind本地行业目录")
        add_node(stage_id, "ChainStage", stage, sector=sector, order=CHAIN_STAGES.index(stage) + 1, taxonomy_source="AlphaMind本地行业目录")
        representative = next(doc["path"] for doc in docs if doc["industry"] == industry)
        evidence = _directory_evidence(representative, industry)
        add_edge(industry_id, "PART_OF_SECTOR", sector_id, evidence, confidence=1.0, label="属于板块")
        add_edge(industry_id, "PART_OF_CHAIN", stage_id, evidence, confidence=0.82, label="所属产业链环节")

    for sector in sorted({classify_sector(industry) for industry in industries}):
        representative = next(doc["path"] for doc in docs if classify_sector(doc["industry"]) == sector)
        for left, right in zip(CHAIN_STAGES, CHAIN_STAGES[1:]):
            left_id = _safe_id("stage", f"{sector}:{left}")
            right_id = _safe_id("stage", f"{sector}:{right}")
            add_node(left_id, "ChainStage", left, sector=sector, order=CHAIN_STAGES.index(left) + 1, taxonomy_source="AlphaMind本地行业目录")
            add_node(right_id, "ChainStage", right, sector=sector, order=CHAIN_STAGES.index(right) + 1, taxonomy_source="AlphaMind本地行业目录")
            chain_evidence = {
                "source_file": representative.as_posix(),
                "line": 0,
                "quote": "AlphaMind通用三段式产业链顺序",
                "method": "chain_stage_taxonomy",
                "taxonomy_source": "AlphaMind本地行业目录",
                **_local_evidence_ids(representative, 0),
            }
            add_edge(left_id, "UPSTREAM_OF", right_id, chain_evidence, confidence=0.75, label="上游传导")

    for company in sorted(company_sources):
        company_id = _safe_id("company", company)
        source_files = sorted(path.as_posix() for path in company_sources[company])
        add_node(company_id, "Company", company, source_files=source_files, industries=sorted(company_industries[company]))
        for industry in sorted(company_industries[company]):
            industry_id = _safe_id("industry", industry)
            source = next(path for path in company_sources[company] if _industry_for(path, roots) == industry)
            add_edge(company_id, "BELONGS_TO", industry_id, _directory_evidence(source, industry), confidence=1.0, label="属于行业")

    company_pattern = _compile_company_pattern(company_sources.keys())
    if company_pattern:
        canonical = {name.lower(): name for name in company_sources}
        for doc in docs:
            source_company = doc["company"]
            source_id = _safe_id("company", source_company)
            heading = ""
            text = doc["path"].read_text(encoding="utf-8", errors="ignore")
            for line_number, raw_line in enumerate(text.splitlines(), 1):
                heading_match = HEADING_RE.match(raw_line)
                if heading_match:
                    heading = heading_match.group(1).strip("* ")
                    continue
                line = raw_line.strip()
                if not line or len(line) > 3000:
                    continue
                mentioned: set[str] = set()
                for match in company_pattern.finditer(line):
                    target_company = canonical.get(match.group(0).lower())
                    if not target_company or target_company == source_company or target_company in mentioned:
                        continue
                    relation = _resolve_relation(line, heading, source_company, target_company, match.start())
                    if relation is None:
                        continue
                    mentioned.add(target_company)
                    relation_type, method, confidence, direction = relation
                    target_id = _safe_id("company", target_company)
                    if direction == "target_to_source":
                        edge_source, edge_target = target_id, source_id
                    else:
                        edge_source, edge_target = source_id, target_id
                    add_edge(
                        edge_source,
                        relation_type,
                        edge_target,
                        {
                            "source_file": doc["path"].as_posix(),
                            "line": line_number,
                            "quote": _evidence_excerpt(line, match.start()),
                            "heading": heading,
                            "method": method,
                            **_local_evidence_ids(doc["path"], line_number),
                        },
                        confidence=confidence,
                        label={
                            "SUPPLIES_TO": "供应/客户",
                            "CONTROLS": "控股/控制",
                            "COMPETES_WITH": "竞争",
                            "PARTNERS_WITH": "合作",
                        }.get(relation_type, relation_type),
                    )

    ordered_nodes = sorted(nodes.values(), key=lambda item: (item["type"], item["name"], item["id"]))
    ordered_edges = sorted(edges.values(), key=lambda item: (item["type"], item["source"], item["target"]))
    counts = Counter(node["type"] for node in ordered_nodes)
    relation_counts = Counter(edge["type"] for edge in ordered_edges)
    return {
        "schema_version": SCHEMA_VERSION,
        "taxonomy": {
            "sector_source": "AlphaMind本地行业目录规则",
            "chain_model": list(CHAIN_STAGES),
            "evidence_policy": "企业间关系必须命中已知企业且保留原文文件、行号和句子",
        },
        "nodes": ordered_nodes,
        "edges": ordered_edges,
        "stats": {
            "source_markdown_files": len(docs),
            "nodes": len(ordered_nodes),
            "edges": len(ordered_edges),
            "sectors": counts["Sector"],
            "industries": counts["Industry"],
            "chain_stages": counts["ChainStage"],
            "companies": counts["Company"],
            "relations": dict(sorted(relation_counts.items())),
        },
    }


def validate_graph(graph: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if graph.get("schema_version") != SCHEMA_VERSION:
        issues.append("unexpected schema_version")
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    node_ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    if len(node_ids) != len(set(node_ids)):
        issues.append("duplicate node ids")
    edge_ids = [edge.get("id") for edge in edges if isinstance(edge, dict)]
    if len(edge_ids) != len(set(edge_ids)):
        issues.append("duplicate edge ids")
    known = set(node_ids)
    for edge in edges:
        if edge.get("source") not in known or edge.get("target") not in known:
            issues.append(f"dangling edge: {edge.get('id')}")
        evidence = edge.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            issues.append(f"edge without evidence: {edge.get('id')}")
        elif edge.get("type") in {"SUPPLIES_TO", "PARTNERS_WITH", "COMPETES_WITH", "CONTROLS"}:
            for item in evidence:
                if not item.get("source_file") or not item.get("line") or not item.get("quote"):
                    issues.append(f"business edge has incomplete evidence: {edge.get('id')}")
                    break
    return sorted(set(issues))


NEO4J_NODE_LABELS = {
    "Sector": "Sector",
    "Industry": "Industry",
    "ChainStage": "ChainStage",
    "Company": "Company",
    "Product": "Product",
    "Material": "Material",
}
NEO4J_RELATION_TYPES = {
    "PART_OF_SECTOR",
    "PART_OF_CHAIN",
    "UPSTREAM_OF",
    "BELONGS_TO",
    "SUPPLIES_TO",
    "PARTNERS_WITH",
    "COMPETES_WITH",
    "CONTROLS",
    "HAS_PRODUCT",
    "USES_MATERIAL",
}


def _neo4j_property_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in value):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_neo4j_import_statements(graph: dict[str, Any], *, replace: bool = True) -> list[dict[str, Any]]:
    """Build an allow-listed, fully parameterized Neo4j import transaction."""
    if graph.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unexpected relation graph schema_version")
    statements: list[dict[str, Any]] = []
    if replace:
        statements.append({"statement": "MATCH (n:AlphaMindEntity) DETACH DELETE n", "parameters": {}})

    grouped_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in graph.get("nodes", []):
        node_type = str(node.get("type", ""))
        label = NEO4J_NODE_LABELS.get(node_type)
        if label is None:
            raise ValueError(f"unsupported Neo4j node type: {node_type}")
        props = {key: _neo4j_property_value(value) for key, value in node.items() if key != "id"}
        props["entity_type"] = node_type
        props["graph_source"] = "alphamind_relation_graph"
        grouped_nodes[label].append({"id": node["id"], "props": props})

    for label in sorted(grouped_nodes):
        statements.append({
            "statement": (
                f"UNWIND $rows AS row MERGE (n:AlphaMindEntity:{label} {{id: row.id}}) "
                "SET n += row.props"
            ),
            "parameters": {"rows": grouped_nodes[label]},
        })

    grouped_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph.get("edges", []):
        relation_type = str(edge.get("type", ""))
        if relation_type not in NEO4J_RELATION_TYPES:
            raise ValueError(f"unsupported Neo4j relation type: {relation_type}")
        evidence = edge.get("evidence") if isinstance(edge.get("evidence"), list) else []
        first = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
        grouped_edges[relation_type].append({
            "id": edge["id"],
            "source": edge["source"],
            "target": edge["target"],
            "label": edge.get("label", relation_type),
            "confidence": float(edge.get("confidence", 0.0)),
            "source_file": first.get("source_file", ""),
            "source_line": int(first.get("line") or 0),
            "evidence_quote": first.get("quote", ""),
            "evidence_method": first.get("method", ""),
            "source_doc_id": first.get("source_doc_id", ""),
            "source_chunk_id": first.get("source_chunk_id", ""),
            "page": int(first.get("page") or 0),
            "taxonomy_source": first.get("taxonomy_source", ""),
            "evidence_count": len(evidence),
            "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            "graph_source": "alphamind_relation_graph",
        })

    for relation_type in sorted(grouped_edges):
        statements.append({
            "statement": (
                "UNWIND $rows AS row "
                "MATCH (source:AlphaMindEntity {id: row.source}) "
                "MATCH (target:AlphaMindEntity {id: row.target}) "
                f"MERGE (source)-[r:{relation_type} {{id: row.id}}]->(target) "
                "SET r.label = row.label, r.confidence = row.confidence, "
                "r.source_file = row.source_file, r.source_line = row.source_line, "
                "r.evidence_quote = row.evidence_quote, r.evidence_method = row.evidence_method, "
                "r.source_doc_id = row.source_doc_id, r.source_chunk_id = row.source_chunk_id, "
                "r.page = row.page, r.taxonomy_source = row.taxonomy_source, "
                "r.evidence_count = row.evidence_count, r.evidence_json = row.evidence_json, "
                "r.graph_source = row.graph_source"
            ),
            "parameters": {"rows": grouped_edges[relation_type]},
        })
    return statements


def neo4j_schema_statements() -> list[dict[str, Any]]:
    return [
        {"statement": "CREATE CONSTRAINT alphamind_entity_id IF NOT EXISTS FOR (n:AlphaMindEntity) REQUIRE n.id IS UNIQUE", "parameters": {}},
        {"statement": "CREATE INDEX alphamind_entity_name IF NOT EXISTS FOR (n:AlphaMindEntity) ON (n.name)", "parameters": {}},
        {"statement": "CREATE INDEX alphamind_entity_type IF NOT EXISTS FOR (n:AlphaMindEntity) ON (n.entity_type)", "parameters": {}},
    ]


def _default_neo4j_requester(endpoint: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Neo4j transaction request failed: {exc}") from exc


def import_graph(
    graph: dict[str, Any],
    *,
    neo4j_url: str,
    username: str,
    password: str,
    database: str = "neo4j",
    replace: bool = True,
    timeout: float = 60.0,
    requester: Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create schema, then atomically replace AlphaMind-owned graph nodes."""
    issues = validate_graph(graph)
    if issues:
        raise ValueError(f"relation graph validation failed: {issues}")
    if not neo4j_url or not username or not password:
        raise ValueError("neo4j_url, username and password are required")
    endpoint = neo4j_url.rstrip("/") + f"/db/{quote(database, safe='')}/tx/commit"
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {auth}"}
    send = requester or _default_neo4j_requester
    payloads = [
        {"statements": neo4j_schema_statements()},
        {"statements": build_neo4j_import_statements(graph, replace=replace)},
    ]
    for payload in payloads:
        response = send(endpoint, payload, headers, timeout)
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("errors"), list)
            or not isinstance(response.get("results"), list)
            or len(response["results"]) != len(payload["statements"])
        ):
            raise RuntimeError("Neo4j import failed: malformed transaction response")
        errors = response["errors"]
        if errors:
            safe_errors = [
                {"code": item.get("code", ""), "message": item.get("message", "")}
                for item in errors
                if isinstance(item, dict)
            ]
            raise RuntimeError(f"Neo4j import failed: {safe_errors}")
    stats = graph.get("stats", {})
    return {
        "status": "pass",
        "endpoint": endpoint,
        "nodes": int(stats.get("nodes", len(graph.get("nodes", [])))),
        "edges": int(stats.get("edges", len(graph.get("edges", [])))),
        "replace": replace,
    }


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and optionally import the AlphaMind sector/supply-chain/company graph.")
    parser.add_argument("--source-root", action="append", required=True, help="Root containing company Markdown files; repeatable.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--import-neo4j", action="store_true", help="Import the validated graph into Neo4j after building it.")
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parents[1] / ".env.phase3-precision"))
    parser.add_argument("--neo4j-url", default="")
    parser.add_argument("--neo4j-user", default="")
    parser.add_argument("--neo4j-password", default="")
    parser.add_argument("--neo4j-database", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    graph = build_graph([Path(item) for item in args.source_root])
    issues = validate_graph(graph)
    output = Path(args.output)
    report = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    quality = {"status": "pass" if not issues else "fail", "issues": issues, "stats": graph["stats"], "graph_path": str(output.resolve())}
    if args.import_neo4j and not issues:
        env = parse_env_file(Path(args.env_file))
        neo4j_result = import_graph(
            graph,
            neo4j_url=args.neo4j_url or env.get("NEO4J_HTTP_URL") or f"http://localhost:{env.get('NEO4J_HTTP_PORT', '7474')}",
            username=args.neo4j_user or env.get("NEO4J_USERNAME", "neo4j"),
            password=args.neo4j_password or env.get("NEO4J_PASSWORD", ""),
            database=args.neo4j_database or env.get("NEO4J_DATABASE", "neo4j"),
            timeout=args.timeout,
        )
        quality["neo4j_import"] = neo4j_result
    report.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(quality, ensure_ascii=False, indent=2))
    return 0 if not issues else 1


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
