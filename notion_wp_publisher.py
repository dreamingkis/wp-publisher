#!/usr/bin/env python3
"""
🚀 노션 → 워드프레스 자동 발행 시스템 v4 (bestar.kr 전용)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v3 대비 변경점:
  ✅ 포트폴리오(CPT) 자동 발행 지원
  ✅ 노션 DB '발행타입' 속성으로 블로그/포트폴리오 분기
  ✅ CPT별 카테고리/태그 택소노미 자동 매핑
  ✅ POST_TYPE_CONFIG로 확장 가능한 구조

트리거: 노션 DB "상태"가 "원고작성완료"인 글 자동 감지
기능:   ✅ WP 자동 발행 (블로그 + 포트폴리오)
        ✅ 대표이미지(썸네일) 자동 설정
        ✅ SEO 메타 자동 생성 (Yoast/RankMath)
        ✅ 카테고리/태그 병렬 처리
        ✅ 노션 상태 → "홈페이지 발행 완료" 자동 업데이트
        ✅ 노션 "워드프레스 URL" 필드 자동 기입
        ✅ 발행 실패 시 노션 상태 → "발행실패" 표시
        ✅ 토글 블록 자식 내용 렌더링
        ✅ 로그 파일 저장 (publisher.log)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
실행 방법:
  python notion_wp_publisher.py              # 1회 실행
  python notion_wp_publisher.py --watch      # 상시 자동 감시 (5분 간격)
  python notion_wp_publisher.py --dry-run    # 실제 발행 없이 동작 확인
"""

import sys
import time
import re
import json
import argparse
import logging
import mimetypes
import os
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor

import requests
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────
# ✅ 설정값 (.env 또는 환경변수에서 로드)
# ─────────────────────────────
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "2aaa9a5b-4f04-80d6-a036-e3abb5c18802")
WP_URL = os.environ.get("WP_URL", "https://bestar.kr")
WP_USER = os.environ.get("WP_USER", "bestarkr")
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]

TRIGGER_STATUS = "원고작성완료"
DONE_STATUS = "홈페이지 발행 완료"
FAIL_STATUS = "발행실패"
REPUBLISH_STATUS = "재발행"
CHECK_INTERVAL = 300  # 5분

# ─────────────────────────────
# ✅ v4 신규: 포스트 타입별 설정
# ─────────────────────────────
# 노션 DB의 '발행타입' 속성값 → WP REST API 엔드포인트 매핑
# 새로운 CPT를 추가하려면 여기에 항목만 추가하면 됩니다.
POST_TYPE_CONFIG = {
    "블로그": {
        "endpoint": "/wp-json/wp/v2/posts",
        "category_taxonomy": "categories",      # WP REST API 택소노미 슬러그
        "tag_taxonomy": "tags",
        "notion_category_prop": "카테고리",       # 노션 DB 속성명
        "notion_tag_prop": "태그",
        "acf_fields": {},                        # 블로그에는 ACF 필드 없음
    },
    "포트폴리오": {
        "endpoint": "/wp-json/wp/v2/portfolio",
        "category_taxonomy": "portfolio_cat",
        "tag_taxonomy": "portfolio_tag",
        "notion_category_prop": "포트폴리오 카테고리",
        "notion_tag_prop": "포트폴리오 태그",
        "acf_fields": {
            # 노션 속성명: WP ACF 필드명
            "클라이언트": "pf_client",
            "교육대상": "pf_target",
            "참여인원": "pf_people",
            "진행시간": "pf_time",
            "일시": "pf_period",
        },
    },
}

# 발행타입이 지정되지 않은 경우 기본값
DEFAULT_POST_TYPE = "블로그"

CATEGORY_MAP = {
    # 노션 카테고리명: WP 카테고리명이 다를 때만 기입
    "갤럽 강점테마": "갤럽 강점테마",
    "강점활용": "강점활용",
    "브랜딩 전략": "브랜딩 전략",
    "퍼스널브랜딩": "퍼스널브랜딩",
}

# WP 인증 토큰 (모듈 로드 시 1회만 계산)
_WP_AUTH = b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode()).decode()

# ─────────────────────────────
# 로그 설정 (콘솔 + 파일)
# ─────────────────────────────
_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publisher.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def wp_headers(content_type="application/json"):
    return {"Authorization": f"Basic {_WP_AUTH}", "Content-Type": content_type}


# ─────────────────────────────
# 🖼️ 대표이미지: 노션 이미지 → WP 미디어 업로드
# ─────────────────────────────
def upload_featured_image(image_url: str, title: str) -> int | None:
    try:
        log.info("  🖼️ 대표이미지 다운로드 중...")
        res = requests.get(image_url, timeout=30)
        res.raise_for_status()
        content_type = res.headers.get("Content-Type", "image/jpeg").split(";")[0]
        ext = (mimetypes.guess_extension(content_type) or ".jpg").replace(".jpe", ".jpg")
        filename = re.sub(r"[^\w가-힣]", "_", title)[:50] + ext

        upload_res = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            headers={
                "Authorization": f"Basic {_WP_AUTH}",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": content_type,
            },
            data=res.content,
            timeout=60,
        )
        upload_res.raise_for_status()
        media_id = upload_res.json()["id"]
        log.info(f"  ✅ 대표이미지 업로드 완료 (미디어 ID: {media_id})")
        return media_id
    except Exception as e:
        log.warning(f"  ⚠️ 대표이미지 업로드 실패 (건너뜀): {e}")
        return None


def extract_first_image_url(blocks: list) -> str:
    for block in blocks:
        if block["type"] == "image":
            bdata = block.get("image", {})
            url = (bdata.get("file") or bdata.get("external") or {}).get("url", "")
            if url:
                return url
    return ""


def get_or_create_wp_term(taxonomy: str, name: str) -> int | None:
    """WP 카테고리/태그 ID 조회 또는 생성. 실패 시 None 반환."""
    endpoint = f"{WP_URL}/wp-json/wp/v2/{taxonomy}"
    try:
        res = requests.get(endpoint, params={"search": name, "per_page": 20}, headers=wp_headers())
        res.raise_for_status()
        items = res.json()
        if isinstance(items, list):
            for item in items:
                if item.get("name") == name:
                    return item["id"]
        # 없으면 신규 생성
        res = requests.post(endpoint, headers=wp_headers(), json={"name": name})
        res.raise_for_status()
        return res.json()["id"]
    except Exception as e:
        log.warning(f"  ⚠️ 태그/카테고리 처리 실패 ({name}): {e}")
        return None


# ─────────────────────────────
# 📝 노션 블록 → HTML 변환
# ─────────────────────────────
def rich_text_to_html(rich_texts: list) -> str:
    result = ""
    for rt in rich_texts:
        text = rt.get("plain_text", "")
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ann = rt.get("annotations", {})
        if ann.get("bold"):          text = f"<strong>{text}</strong>"
        if ann.get("italic"):        text = f"<em>{text}</em>"
        if ann.get("code"):          text = f"<code>{text}</code>"
        if ann.get("strikethrough"): text = f"<s>{text}</s>"
        link = rt.get("href")
        if link: text = f'<a href="{link}" target="_blank">{text}</a>'
        result += text
    return result


def fetch_all_blocks(notion: Client, block_id: str) -> list:
    """페이지네이션 처리하여 모든 자식 블록을 가져옴."""
    blocks = []
    cursor = None
    while True:
        resp = notion.blocks.children.list(block_id=block_id, start_cursor=cursor, page_size=100)
        blocks.extend(resp["results"])
        if not resp["has_more"]:
            break
        cursor = resp["next_cursor"]
    return blocks


def _get_children_html(block: dict, notion: Client) -> str:
    """has_children이 True인 블록의 자식 콘텐츠를 재귀적으로 가져옴."""
    if not block.get("has_children", False):
        return ""
    children = fetch_all_blocks(notion, block["id"])
    return blocks_to_html(children, notion)


def blocks_to_html(blocks: list, notion: Client) -> str:
    """블록 리스트 → HTML (중첩 블록 재귀 지원)."""
    html_parts = []
    list_buffer = []
    list_type = None

    def flush_list():
        nonlocal list_type, list_buffer
        if list_buffer:
            tag = list_type or "ul"
            html_parts.append(f"<{tag}>")
            for li in list_buffer:
                html_parts.append(f"  <li>{li}</li>")
            html_parts.append(f"</{tag}>")
            list_buffer.clear()
            list_type = None

    for block in blocks:
        btype = block["type"]
        bdata = block.get(btype, {})
        text = rich_text_to_html(bdata.get("rich_text", []))
        has_children = block.get("has_children", False)

        if btype not in ("bulleted_list_item", "numbered_list_item"):
            flush_list()

        if btype == "paragraph":
            inner = text
            if has_children:
                inner += _get_children_html(block, notion)
            if inner.strip():
                html_parts.append(f"<p>{inner}</p>")
        elif btype in ("heading_1", "heading_2", "heading_3"):
            lvl = btype[-1]
            html_parts.append(f"<h{lvl}>{text}</h{lvl}>")
            if has_children:
                html_parts.append(_get_children_html(block, notion))
        elif btype == "bulleted_list_item":
            if list_type != "ul":
                flush_list()
            list_type = "ul"
            li_content = text
            if has_children:
                li_content += _get_children_html(block, notion)
            list_buffer.append(li_content)
        elif btype == "numbered_list_item":
            if list_type != "ol":
                flush_list()
            list_type = "ol"
            li_content = text
            if has_children:
                li_content += _get_children_html(block, notion)
            list_buffer.append(li_content)
        elif btype == "quote":
            inner = text
            if has_children:
                inner += _get_children_html(block, notion)
            html_parts.append(f"<blockquote><p>{inner}</p></blockquote>")
        elif btype == "divider":
            html_parts.append("<hr/>")
        elif btype == "code":
            code = bdata.get("rich_text", [{}])[0].get("plain_text", "")
            lang = bdata.get("language", "")
            html_parts.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
        elif btype == "table":
            rows = fetch_all_blocks(notion, block["id"])
            html_parts.append("<table>")
            for i, row in enumerate(rows):
                cells = row.get("table_row", {}).get("cells", [])
                tag = "th" if i == 0 else "td"
                html_parts.append("<tr>")
                for cell in cells:
                    html_parts.append(f"  <{tag}>{rich_text_to_html(cell)}</{tag}>")
                html_parts.append("</tr>")
            html_parts.append("</table>")
        elif btype == "image":
            url = (bdata.get("file") or bdata.get("external") or {}).get("url", "")
            cap = rich_text_to_html(bdata.get("caption", []))
            if url:
                html_parts.append(f'<figure><img src="{url}" alt="{cap}"/>')
                if cap:
                    html_parts.append(f"<figcaption>{cap}</figcaption>")
                html_parts.append("</figure>")
        elif btype == "callout":
            icon = bdata.get("icon", {}).get("emoji", "💡")
            inner = text
            if has_children:
                inner += _get_children_html(block, notion)
            html_parts.append(f'<div class="callout"><span>{icon}</span><div>{inner}</div></div>')
        elif btype == "toggle":
            children_html = _get_children_html(block, notion)
            html_parts.append(f"<details><summary>{text}</summary>{children_html}</details>")
        elif btype == "column_list":
            # column_list의 자식은 column 블록들 → 각 column의 내용을 순서대로 렌더링
            columns = fetch_all_blocks(notion, block["id"])
            html_parts.append('<div class="wp-block-columns">')
            for col in columns:
                html_parts.append('<div class="wp-block-column">')
                col_children = fetch_all_blocks(notion, col["id"])
                html_parts.append(blocks_to_html(col_children, notion))
                html_parts.append("</div>")
            html_parts.append("</div>")
        elif btype == "to_do":
            checked = bdata.get("checked", False)
            marker = "☑" if checked else "☐"
            html_parts.append(f"<p>{marker} {text}</p>")
        elif btype == "synced_block":
            # 동기화 블록: 자식 콘텐츠를 그대로 렌더링
            if has_children:
                html_parts.append(_get_children_html(block, notion))
        elif btype == "bookmark":
            url = bdata.get("url", "")
            cap = rich_text_to_html(bdata.get("caption", []))
            if url:
                label = cap if cap else url
                html_parts.append(f'<p><a href="{url}" target="_blank">{label}</a></p>')
        elif btype == "embed":
            url = bdata.get("url", "")
            if url:
                html_parts.append(f'<figure><iframe src="{url}" frameborder="0"></iframe></figure>')
        elif btype == "video":
            url = (bdata.get("file") or bdata.get("external") or {}).get("url", "")
            if url:
                if "youtube" in url or "youtu.be" in url or "vimeo" in url:
                    html_parts.append(f'<figure><iframe src="{url}" frameborder="0" allowfullscreen></iframe></figure>')
                else:
                    html_parts.append(f'<figure><video src="{url}" controls></video></figure>')
        elif btype == "file" or btype == "pdf":
            url = (bdata.get("file") or bdata.get("external") or {}).get("url", "")
            cap = rich_text_to_html(bdata.get("caption", []))
            if url:
                label = cap if cap else (bdata.get("name", "") or "파일 다운로드")
                html_parts.append(f'<p><a href="{url}" target="_blank">{label}</a></p>')
        elif btype == "link_preview":
            url = bdata.get("url", "")
            if url:
                html_parts.append(f'<p><a href="{url}" target="_blank">{url}</a></p>')
        else:
            log.debug(f"  ⚠️ 미지원 블록 타입: {btype}")

    flush_list()
    return "\n".join(html_parts)


def notion_blocks_to_html(notion: Client, page_id: str) -> tuple[str, list]:
    blocks = fetch_all_blocks(notion, page_id)
    if not blocks:
        log.warning(f"  ⚠️ 페이지 블록이 0개입니다 (page_id: {page_id}). "
                     "노션 Integration의 'Read content' 권한을 확인하세요.")
    else:
        block_types = [b["type"] for b in blocks]
        log.info(f"  📦 블록 {len(blocks)}개 로드됨: {dict((t, block_types.count(t)) for t in set(block_types))}")
    html = blocks_to_html(blocks, notion)
    if not html.strip():
        log.warning(f"  ⚠️ 변환된 HTML 본문이 비어 있습니다! 블록 수: {len(blocks)}")
    else:
        log.info(f"  📄 HTML 변환 완료: {len(html)}자")
    return html, blocks


# ─────────────────────────────
# ❓ FAQ 스키마 자동 생성
# ─────────────────────────────
def extract_faq_schema(blocks: list) -> str:
    faq_items = []
    current_q = None
    for block in blocks:
        if block["type"] not in ("paragraph", "heading_1", "heading_2", "heading_3"):
            continue
        bdata = block.get(block["type"], {})
        text = "".join(rt.get("plain_text", "") for rt in bdata.get("rich_text", [])).strip()
        if not text:
            continue
        if re.match(r"^Q\d*[.:]\s+", text, re.IGNORECASE):
            current_q = re.sub(r"^Q\d*[.:]\s+", "", text, flags=re.IGNORECASE).strip()
        elif re.match(r"^A[.:]\s+", text, re.IGNORECASE) and current_q:
            answer = re.sub(r"^A[.:]\s+", "", text, flags=re.IGNORECASE).strip()
            faq_items.append({"q": current_q, "a": answer})
            current_q = None

    if not faq_items:
        return ""

    schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item["q"],
                "acceptedAnswer": {"@type": "Answer", "text": item["a"]},
            }
            for item in faq_items
        ],
    }
    log.info(f"  ❓ FAQ 스키마 감지: {len(faq_items)}개 항목")
    return f'\n<script type="application/ld+json">\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n</script>'


# ─────────────────────────────
# 🔍 SEO 메타 생성
# ─────────────────────────────
def generate_seo_meta(title: str, content_html: str, meta_desc_input: str = "", focus_kw_input: str = "") -> dict:
    if meta_desc_input:
        desc = meta_desc_input
    else:
        plain = re.sub(r"<[^>]+>", "", content_html)
        plain = re.sub(r"\s+", " ", plain).strip()
        desc = plain[:150] + "..." if len(plain) > 150 else plain

    if focus_kw_input:
        keyword = focus_kw_input
    else:
        keyword = title.split(",")[0].strip() if "," in title else title.split()[0]

    return {"meta_description": desc, "focus_keyword": keyword}


# ─────────────────────────────
# ✅ v4 신규: 발행타입 결정 헬퍼
# ─────────────────────────────
def get_post_type_from_page(props: dict) -> str:
    """노션 페이지 속성에서 발행타입을 읽어옵니다.
    
    '발행타입' Select 속성이 있으면 그 값을 사용하고,
    없으면 DEFAULT_POST_TYPE (블로그)를 반환합니다.
    """
    post_type_prop = props.get("발행타입", {})
    select_val = post_type_prop.get("select")
    if select_val and select_val.get("name"):
        pt = select_val["name"]
        if pt in POST_TYPE_CONFIG:
            return pt
        else:
            log.warning(f"  ⚠️ 알 수 없는 발행타입 '{pt}' → 기본값 '{DEFAULT_POST_TYPE}' 사용")
    return DEFAULT_POST_TYPE


# ─────────────────────────────
# 🔄 WP 기존 포스트 ID 조회 (재발행용)
# ─────────────────────────────
def resolve_wp_post_id(wp_url: str, endpoint: str) -> int | None:
    """워드프레스 URL에서 기존 포스트 ID를 조회합니다."""
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(wp_url)

    # Case 1: ?p=123 형식
    qs = parse_qs(parsed.query)
    if "p" in qs:
        try:
            return int(qs["p"][0])
        except (ValueError, IndexError):
            pass

    # Case 2: /slug/ 형식 → REST API로 슬러그 기반 조회
    path = parsed.path.strip("/")
    slug = path.split("/")[-1] if path else ""

    if slug:
        try:
            res = requests.get(
                f"{WP_URL}{endpoint}",
                params={"slug": slug, "status": "publish,draft,pending,private"},
                headers=wp_headers(),
            )
            res.raise_for_status()
            posts = res.json()
            if posts:
                return posts[0]["id"]
        except Exception as e:
            log.warning(f"  ⚠️ 슬러그 기반 포스트 조회 실패: {e}")

    return None


# ─────────────────────────────
# 📤 WP 발행 (v4: 포스트 타입 분기, 재발행 지원)
# ─────────────────────────────
def publish_to_wp(page: dict, notion: Client, dry_run: bool = False, existing_post_id: int | None = None) -> dict:
    props = page.get("properties", {})
    page_id = page["id"]

    title = "".join(rt["plain_text"] for rt in props.get("이름", {}).get("title", []))

    # ── v4: 발행타입 결정 → 설정 로드 ──
    post_type = get_post_type_from_page(props)
    pt_config = POST_TYPE_CONFIG[post_type]
    endpoint = pt_config["endpoint"]
    cat_taxonomy = pt_config["category_taxonomy"]
    tag_taxonomy = pt_config["tag_taxonomy"]
    notion_cat_prop = pt_config["notion_category_prop"]
    notion_tag_prop = pt_config["notion_tag_prop"]

    # ── 발행타입에 맞는 노션 속성에서 카테고리/태그 읽기 ──
    cat_name = ((props.get(notion_cat_prop) or {}).get("select") or {}).get("name", "")
    wp_cat = CATEGORY_MAP.get(cat_name, cat_name)

    kw_items = (props.get(notion_tag_prop) or {}).get("multi_select", [])
    tag_names = [kw["name"] for kw in kw_items]
    if "강점" in cat_name:
        tag_names += ["갤럽강점", "CliftonStrengths", "강점코칭"]
    tag_names = list(dict.fromkeys(tag_names))  # 중복 제거

    meta_desc_input = "".join(
        rt.get("plain_text", "") for rt in ((props.get("메타디스크립션") or {}).get("rich_text") or [])
    ).strip()
    focus_kw_input = "".join(
        rt.get("plain_text", "") for rt in ((props.get("포커스키워드") or {}).get("rich_text") or [])
    ).strip()

    log.info(f"  📝 블록 변환 중: {title}")
    log.info(f"  📦 발행타입: {post_type} → {endpoint}")
    content_html, blocks = notion_blocks_to_html(notion, page_id)

    if not content_html.strip():
        raise ValueError(f"본문 HTML이 비어 있습니다. 노션 페이지에 콘텐츠가 없거나 "
                         f"Integration 'Read content' 권한을 확인하세요. (page_id: {page_id})")

    seo = generate_seo_meta(title, content_html, meta_desc_input, focus_kw_input)
    log.info(f"  🔍 SEO 키워드: {seo['focus_keyword']}")

    # 대표이미지 (1순위: DB URL 필드, 2순위: 본문 첫 이미지 — HTML에서 추출하여 중첩 블록도 감지)
    thumb_url = (props.get("대표이미지") or {}).get("url") or ""
    if not thumb_url:
        img_match = re.search(r'<img\s+[^>]*src="([^"]+)"', content_html)
        if img_match:
            thumb_url = img_match.group(1)
    featured_media_id = None
    if thumb_url and not dry_run:
        featured_media_id = upload_featured_image(thumb_url, title)
    elif not thumb_url:
        log.info("  ℹ️ 대표이미지 없음")

    # 카테고리/태그 병렬 조회·생성 (v4: 택소노미 동적 매핑)
    cat_id, tag_ids = None, []
    if not dry_run:
        with ThreadPoolExecutor(max_workers=8) as executor:
            cat_future = executor.submit(get_or_create_wp_term, cat_taxonomy, wp_cat) if wp_cat else None
            tag_futures = [executor.submit(get_or_create_wp_term, tag_taxonomy, t) for t in tag_names]

            if cat_future:
                cat_id = cat_future.result()
            tag_ids = [r for f in tag_futures if (r := f.result()) is not None]

    faq_schema = extract_faq_schema(blocks)
    final_content = content_html + faq_schema if faq_schema else content_html

    # ── v4: 포스트 데이터 구성 (택소노미 키 동적) ──
    post_data = {
        "title": title,
        "content": final_content,
        "status": "publish",
        "meta": {
            "_yoast_wpseo_metadesc": seo["meta_description"],
            "_yoast_wpseo_focuskw": seo["focus_keyword"],
            "rank_math_description": seo["meta_description"],
            "rank_math_focus_keyword": seo["focus_keyword"],
        },
    }

    # 카테고리/태그 키 이름이 post_type에 따라 다름
    if cat_id:
        post_data[cat_taxonomy] = [cat_id]
    if tag_ids:
        post_data[tag_taxonomy] = tag_ids

    if featured_media_id:
        post_data["featured_media"] = featured_media_id

    # ── v4: ACF 커스텀 필드 처리 ──
    acf_fields = pt_config.get("acf_fields", {})
    if acf_fields:
        acf_data = {}
        for notion_prop, wp_field in acf_fields.items():
            value = "".join(
                rt.get("plain_text", "")
                for rt in ((props.get(notion_prop) or {}).get("rich_text") or [])
            ).strip()
            if value:
                acf_data[wp_field] = value
        if acf_data:
            post_data["acf"] = acf_data
            log.info(f"  📋 ACF 필드: {acf_data}")

    if dry_run:
        action = "재발행" if existing_post_id else "발행"
        log.info(f"  [DRY-RUN] {action} 생략 — 제목: {title}, 발행타입: {post_type}, 카테고리: {wp_cat}, 태그: {tag_names}")
        return {"link": "https://dry-run.local/"}

    if existing_post_id:
        log.info(f"  🔄 기존 포스트 업데이트 중 (ID: {existing_post_id})")
        res = requests.post(f"{WP_URL}{endpoint}/{existing_post_id}", headers=wp_headers(), json=post_data)
    else:
        res = requests.post(f"{WP_URL}{endpoint}", headers=wp_headers(), json=post_data)
    res.raise_for_status()
    return res.json()


def update_notion_status(notion: Client, page_id: str, status: str, wp_url: str = "") -> None:
    props: dict = {"상태": {"status": {"name": status}}}
    if wp_url:
        props["워드프레스 URL"] = {"url": wp_url}
    notion.pages.update(page_id=page_id, properties=props)


def fetch_pending_pages(notion: Client) -> list:
    """페이지네이션 처리로 발행/재발행 대기 중인 글을 모두 가져옴 (100건 초과 대응)."""
    results = []
    cursor = None
    while True:
        resp = notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={
                "or": [
                    {"property": "상태", "status": {"equals": TRIGGER_STATUS}},
                    {"property": "상태", "status": {"equals": REPUBLISH_STATUS}},
                ]
            },
            start_cursor=cursor,
            page_size=100,
        )
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return results


def run_once(notion: Client, dry_run: bool = False) -> None:
    pages = fetch_pending_pages(notion)
    if not pages:
        log.info("📭 발행 대기 중인 글이 없습니다.")
        return

    log.info(f"📋 발행 대기 글 {len(pages)}개 발견")
    success, fail = 0, 0

    for page in pages:
        props = page["properties"]
        title = "".join(rt["plain_text"] for rt in props.get("이름", {}).get("title", []))
        page_id = page["id"]
        page_status = (props.get("상태", {}).get("status") or {}).get("name", "")
        is_republish = (page_status == REPUBLISH_STATUS)

        log.info(f"\n▶ {'재발행' if is_republish else '신규 발행'} 처리 중: {title}")

        try:
            existing_post_id = None
            if is_republish:
                wp_url_existing = (props.get("워드프레스 URL") or {}).get("url", "")
                if not wp_url_existing:
                    raise ValueError("재발행 실패: 노션에 워드프레스 URL이 없습니다")
                post_type = get_post_type_from_page(props)
                endpoint = POST_TYPE_CONFIG[post_type]["endpoint"]
                existing_post_id = resolve_wp_post_id(wp_url_existing, endpoint)
                if not existing_post_id:
                    raise ValueError(f"재발행 실패: 기존 포스트를 찾을 수 없습니다 (URL: {wp_url_existing})")

            wp_post = publish_to_wp(page, notion, dry_run=dry_run, existing_post_id=existing_post_id)
            wp_url = wp_post["link"]
            action_name = "재발행" if is_republish else "발행"
            log.info(f"  ✅ WP {action_name} 완료: {wp_url}")

            if not dry_run:
                update_notion_status(notion, page_id, DONE_STATUS, wp_url)
                log.info(f"  ✅ 노션 상태 → '{DONE_STATUS}' 업데이트 완료")
            success += 1

        except Exception as e:
            log.error(f"  ❌ 오류 발생: {e}", exc_info=True)
            if not dry_run:
                try:
                    update_notion_status(notion, page_id, FAIL_STATUS)
                    log.info(f"  ⚠️ 노션 상태 → '{FAIL_STATUS}' 표시 완료")
                except Exception:
                    pass
            fail += 1

    log.info(f"\n📊 완료 — 성공: {success}건, 실패: {fail}건")


def main():
    parser = argparse.ArgumentParser(description="노션 → WP 자동 발행 v4 (포트폴리오 지원)")
    parser.add_argument("--watch", action="store_true", help="상시 감시 모드 (5분 간격)")
    parser.add_argument("--dry-run", action="store_true", help="실제 발행 없이 동작 확인")
    args = parser.parse_args()

    notion = Client(auth=NOTION_TOKEN)

    if args.dry_run:
        log.info("🧪 DRY-RUN 모드 — 실제 발행/노션 업데이트 없이 실행합니다")

    if args.watch:
        log.info(f"👀 상시 감시 모드 시작 (매 {CHECK_INTERVAL // 60}분 체크)")
        log.info("  종료하려면 Ctrl + C")
        while True:
            try:
                run_once(notion, dry_run=args.dry_run)
            except KeyboardInterrupt:
                log.info("👋 감시 모드 종료")
                sys.exit(0)
            except Exception as e:
                log.error(f"실행 오류: {e}", exc_info=True)
            time.sleep(CHECK_INTERVAL)
    else:
        run_once(notion, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
