"""쇼핑리스트 앱 (Streamlit) — PRD.md 참고."""

import html
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

import streamlit as st

# ---------------------------------------------------------------------------
# 1) 상수 / 설정
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
# 테스트 등에서 저장 위치를 바꿀 수 있도록 환경변수로 덮어쓸 수 있음
DATA_DIR = Path(os.environ.get("SHOPPING_DATA_DIR") or BASE_DIR / "data")
DATA_PATH = DATA_DIR / "shopping_list.json"

FILTER_ALL = "전체"
FILTER_TODO = "미완료"
FILTER_DONE = "완료"
FILTER_OPTIONS = [FILTER_ALL, FILTER_TODO, FILTER_DONE]

QTY_MIN = 1
QTY_MAX = 999

# Streamlit 마크다운에서 서식으로 해석될 수 있는 문자
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!~|<>$:])")

# 같은 서버 프로세스 안의 모든 세션(탭·기기)이 파일을 순서대로 읽고 쓰도록 보호
_FILE_LOCK = threading.RLock()

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 2) 데이터 계층
# ---------------------------------------------------------------------------
def _to_qty(value) -> int:
    try:
        qty = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return QTY_MIN
    return min(max(qty, QTY_MIN), QTY_MAX)


def normalize_items(raw: list) -> tuple[list[dict], bool]:
    """파일에서 읽은 항목을 검증·보정한다. (보정된 목록, 변경 여부)를 반환."""
    items: list[dict] = []
    seen_ids: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        name = str(name).strip() if name is not None else ""
        if not name:
            continue
        item_id = str(entry.get("id") or "")
        if not item_id or item_id in seen_ids:
            item_id = str(uuid.uuid4())
        seen_ids.add(item_id)
        items.append(
            {
                "id": item_id,
                "name": name,
                "quantity": _to_qty(entry.get("quantity")),
                "checked": bool(entry.get("checked", False)),
                "created_at": str(entry.get("created_at") or ""),
            }
        )
    return items, items != raw


def _quarantine_corrupt_file() -> None:
    """손상된 저장 파일을 백업 이름으로 옮기고 사용자에게 알린다."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA_DIR / f"shopping_list.corrupt-{stamp}.json"
    try:
        os.replace(DATA_PATH, backup)
    except OSError:
        return
    save_items([])
    st.session_state["load_warning"] = (
        f"저장 파일이 손상되어 `{backup.name}`(으)로 백업하고 빈 리스트로 시작합니다."
    )


def load_items() -> list[dict]:
    """JSON 파일에서 아이템 목록을 읽는다.

    - 파일이 없으면 빈 파일을 만든다.
    - JSON이 깨졌거나 리스트가 아니면 원본을 백업한 뒤 빈 리스트로 시작한다.
    - 형식이 잘못된 항목은 보정하거나 건너뛰고, 보정 결과를 다시 저장한다.
    """
    with _FILE_LOCK:
        if not DATA_PATH.exists():
            save_items([])
            return []
        try:
            raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _quarantine_corrupt_file()
            return []
        except OSError:
            return []
        if not isinstance(raw, list):
            _quarantine_corrupt_file()
            return []
        items, changed = normalize_items(raw)
        if changed:
            save_items(items)
        return items


def save_items(items: list[dict]) -> None:
    """아이템 목록을 원자적으로 저장한다 (임시 파일에 쓴 뒤 교체)."""
    with _FILE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = DATA_PATH.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DATA_PATH)


def mutate(change: Callable[[list[dict]], T]) -> T:
    """파일의 최신 목록을 읽고 → 변경을 적용하고 → 저장한다 (read-modify-write).

    다른 탭·기기에서 저장한 내용을 덮어쓰지 않도록 모든 변경은 이 함수를 거친다.
    """
    with _FILE_LOCK:
        items = load_items()
        result = change(items)
        save_items(items)
    st.session_state["items"] = items
    return result


# ---------------------------------------------------------------------------
# 3) 상태 초기화
# ---------------------------------------------------------------------------
def init_state() -> None:
    """매 실행마다 파일에서 최신 목록을 읽는다 (다른 화면의 변경도 반영)."""
    st.session_state["items"] = load_items()
    st.session_state.setdefault("editing_id", None)
    st.session_state.setdefault("edit_error", None)
    st.session_state.setdefault("undo", None)
    if find_item(st.session_state["editing_id"]) is None:
        st.session_state["editing_id"] = None


def _find(items: list[dict], item_id: str | None) -> dict | None:
    for item in items:
        if item["id"] == item_id:
            return item
    return None


def find_item(item_id: str | None) -> dict | None:
    return _find(st.session_state["items"], item_id)


# ---------------------------------------------------------------------------
# 4) 비즈니스 로직
# ---------------------------------------------------------------------------
def add_item(name: str, quantity: int = 1) -> bool:
    """새 아이템을 리스트 맨 뒤에 추가한다. 이름이 비어 있으면 False."""
    name = name.strip()
    if not name:
        return False
    new_item = {
        "id": str(uuid.uuid4()),
        "name": name,
        "quantity": _to_qty(quantity),
        "checked": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    mutate(lambda items: items.append(new_item))
    return True


def update_item(item_id: str, name: str, quantity: int) -> bool:
    """id에 해당하는 아이템의 이름/수량을 수정한다. 이름이 비었거나 없는 아이템이면 False."""
    name = name.strip()
    if not name:
        return False

    def change(items: list[dict]) -> bool:
        item = _find(items, item_id)
        if item is None:
            return False
        item["name"] = name
        item["quantity"] = _to_qty(quantity)
        return True

    return mutate(change)


def delete_item(item_id: str) -> list[tuple[int, dict]]:
    """id에 해당하는 아이템을 삭제한다. 되돌리기용 (위치, 아이템) 목록을 반환."""

    def change(items: list[dict]) -> list[tuple[int, dict]]:
        removed = [(i, item) for i, item in enumerate(items) if item["id"] == item_id]
        items[:] = [item for item in items if item["id"] != item_id]
        return removed

    if st.session_state["editing_id"] == item_id:
        st.session_state["editing_id"] = None
    return mutate(change)


def set_checked(item_id: str, checked: bool) -> None:
    """id에 해당하는 아이템의 체크 상태를 지정한다."""

    def change(items: list[dict]) -> None:
        item = _find(items, item_id)
        if item is not None:
            item["checked"] = checked

    mutate(change)


def clear_checked() -> list[tuple[int, dict]]:
    """체크된(구매 완료) 아이템을 모두 삭제한다. 되돌리기용 (위치, 아이템) 목록을 반환."""

    def change(items: list[dict]) -> list[tuple[int, dict]]:
        removed = [(i, item) for i, item in enumerate(items) if item["checked"]]
        items[:] = [item for item in items if not item["checked"]]
        return removed

    removed = mutate(change)
    if st.session_state["editing_id"] in {item["id"] for _, item in removed}:
        st.session_state["editing_id"] = None
    return removed


def restore_items(entries: list[tuple[int, dict]]) -> None:
    """삭제했던 아이템을 원래 위치에 되돌린다 (이미 있는 id는 건너뜀)."""

    def change(items: list[dict]) -> None:
        existing = {item["id"] for item in items}
        for index, item in sorted(entries, key=lambda e: e[0]):
            if item["id"] not in existing:
                items.insert(min(index, len(items)), item)

    mutate(change)


# --- 편집 모드 콜백 ---------------------------------------------------------
def _seed_edit_fields(item: dict) -> None:
    st.session_state[f"edit_name_{item['id']}"] = item["name"]
    st.session_state[f"edit_qty_{item['id']}"] = int(item["quantity"])


def start_edit(item_id: str) -> None:
    st.session_state["editing_id"] = item_id
    st.session_state["edit_error"] = None
    item = find_item(item_id)
    if item is not None:
        _seed_edit_fields(item)


def save_edit(item_id: str) -> None:
    clear_undo()
    name = st.session_state.get(f"edit_name_{item_id}", "")
    qty = st.session_state.get(f"edit_qty_{item_id}", QTY_MIN)
    if update_item(item_id, name, qty):
        st.session_state["editing_id"] = None
        st.session_state["edit_error"] = None
    elif find_item(item_id) is None:  # 다른 화면에서 삭제됨
        st.session_state["editing_id"] = None
    else:
        st.session_state["edit_error"] = item_id


def cancel_edit() -> None:
    st.session_state["editing_id"] = None
    st.session_state["edit_error"] = None


# ---------------------------------------------------------------------------
# 5) 화면 렌더링
# ---------------------------------------------------------------------------
CSS = """
<style>
/* 아이템 행·헤더·입력 행: 좁은 화면에서도 컬럼이 세로로 쌓이지 않게 */
[class*="st-key-row_"] [data-testid="stHorizontalBlock"],
.st-key-header [data-testid="stHorizontalBlock"],
.st-key-undo_bar [data-testid="stHorizontalBlock"],
[data-testid="stForm"] [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap;
    gap: 0.5rem;
}
[class*="st-key-row_"] [data-testid="stColumn"],
.st-key-header [data-testid="stColumn"],
.st-key-undo_bar [data-testid="stColumn"],
[data-testid="stForm"] [data-testid="stColumn"] {
    min-width: 0;
}
/* 아이템 행 카드 */
[class*="st-key-row_"] {
    padding: 0.25rem 0.5rem;
    gap: 0.25rem;
}
[class*="st-key-row_"] [data-testid="stCheckbox"] {
    margin: 0;
}
/* 체크박스·✏️·🗑️ 컬럼: 터치하기 쉬운 고정 폭(2.75rem ≈ 44px) */
[class*="st-key-row_"] [data-testid="stColumn"]:has([data-testid="stCheckbox"]),
[class*="st-key-row_"] [data-testid="stColumn"]:has([data-testid="stBaseButton-tertiary"]) {
    flex: 0 0 2.75rem !important;
    width: 2.75rem !important;
}
[class*="st-key-row_"] [data-testid="stBaseButton-tertiary"] {
    min-width: 2.75rem;
    min-height: 2.75rem;
    justify-content: center;
}
[class*="st-key-row_"] [data-testid="stCheckbox"] label {
    min-width: 2.75rem;
    min-height: 2.75rem;
    justify-content: center;
    align-items: center;
}
/* Streamlit 기본 마크다운 하단 음수 여백(-1rem) 제거 → 세로 가운데 정렬 */
[class*="st-key-row_"] [data-testid="stMarkdownContainer"],
.st-key-header [data-testid="stMarkdownContainer"],
.st-key-undo_bar [data-testid="stMarkdownContainer"] {
    margin-bottom: 0;
}
/* 아이템 이름 / 수량 배지 */
.item-line {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 0;
}
.item-name {
    overflow-wrap: anywhere;
    line-height: 1.4;
}
.item-name.done {
    text-decoration: line-through;
    opacity: 0.45;
}
.qty-badge {
    flex-shrink: 0;
    font-size: 0.75rem;
    font-weight: 600;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    background: rgba(128, 128, 128, 0.18);
    opacity: 0.85;
}
.qty-badge.done {
    opacity: 0.4;
}
/* 헤더 진행 현황 */
.progress-count {
    text-align: right;
    font-weight: 600;
    white-space: nowrap;
    opacity: 0.8;
}
/* 되돌리기 바 */
.st-key-undo_bar {
    padding: 0.25rem 0.75rem;
    border-radius: 0.5rem;
    background: rgba(128, 128, 128, 0.12);
}
/* 추가·되돌리기 버튼 글자, 입력 라벨이 좁은 화면에서 잘리거나 줄바꿈되지 않게 */
[data-testid="stForm"] button,
.st-key-undo_bar button {
    white-space: nowrap;
    padding-left: 0.5rem;
    padding-right: 0.5rem;
}
[data-testid="stForm"] [data-testid="stWidgetLabel"] p {
    white-space: nowrap;
}
.st-key-undo_bar [data-testid="stColumn"]:has(button) {
    flex: 0 0 auto !important;
    width: auto !important;
}
/* 모바일: 제목 크기 축소 */
@media (max-width: 640px) {
    .st-key-header h1 {
        font-size: 1.75rem;
        white-space: nowrap;
    }
}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def escape_md(text: str) -> str:
    """사용자 입력이 마크다운 서식으로 해석되지 않도록 이스케이프한다."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def notify(message: str) -> None:
    """콜백에서 호출: 다음 렌더링 때 토스트로 보여줄 메시지를 예약한다."""
    st.session_state["toast"] = message


def clear_undo() -> None:
    st.session_state["undo"] = None


# --- UI 콜백 (비즈니스 로직 호출 + 알림/되돌리기 예약) ----------------------
def submit_add() -> None:
    name = st.session_state.get("new_name", "")
    ok = add_item(name, st.session_state.get("new_qty", QTY_MIN))
    st.session_state["add_error"] = not ok
    if ok:
        clear_undo()
        notify(f"🛒 '{escape_md(name.strip())}' 추가했어요")


def on_toggle(item_id: str) -> None:
    clear_undo()
    set_checked(item_id, bool(st.session_state.get(f"chk_{item_id}", False)))


def on_delete(item_id: str) -> None:
    removed = delete_item(item_id)
    if removed:
        st.session_state["undo"] = {
            "entries": removed,
            "message": f"🗑️ '{escape_md(removed[0][1]['name'])}' 삭제했어요",
        }


def on_clear_checked() -> None:
    removed = clear_checked()
    if removed:
        st.session_state["undo"] = {
            "entries": removed,
            "message": f"🧹 완료한 항목 {len(removed)}개를 삭제했어요",
        }


def on_undo() -> None:
    undo = st.session_state.get("undo")
    if undo:
        restore_items(undo["entries"])
        notify("↩️ 되돌렸어요")
    clear_undo()


def on_filter_change() -> None:
    # 보기를 바꾸면 편집 중이던 행이 사라질 수 있으므로 편집 모드를 종료
    cancel_edit()


# --- 렌더링 ----------------------------------------------------------------
def render_header() -> None:
    items = st.session_state["items"]
    total = len(items)
    done = sum(1 for i in items if i["checked"])
    with st.container(key="header"):
        col_title, col_count = st.columns([3, 1], vertical_alignment="bottom")
        col_title.title("🛒 쇼핑리스트")
        col_count.markdown(
            f'<div class="progress-count">{done} / {total} 완료</div>',
            unsafe_allow_html=True,
        )
        st.progress(done / total if total else 0.0)


def render_input_form() -> None:
    with st.form("add_form", clear_on_submit=True, border=False):
        col_name, col_qty, col_btn = st.columns([5, 2, 1.6], vertical_alignment="bottom")
        col_name.text_input("아이템 이름", placeholder="예: 우유", key="new_name")
        col_qty.number_input(
            "수량", min_value=QTY_MIN, max_value=QTY_MAX, value=QTY_MIN, step=1,
            key="new_qty", help=f"{QTY_MIN}~{QTY_MAX} 사이 숫자",
        )
        col_btn.form_submit_button("추가", on_click=submit_add, type="primary",
                                   width="stretch")
    if st.session_state.pop("add_error", False):
        st.warning("아이템 이름을 입력해주세요.")


def render_filter() -> str:
    return st.radio("보기", FILTER_OPTIONS, horizontal=True, key="filter",
                    label_visibility="collapsed", on_change=on_filter_change)


def render_undo_bar() -> None:
    undo = st.session_state.get("undo")
    if not undo:
        return
    with st.container(key="undo_bar"):
        col_msg, col_btn = st.columns([4, 1.4], vertical_alignment="center")
        col_msg.markdown(undo["message"])
        col_btn.button("↩️ 되돌리기", key="undo_btn", on_click=on_undo, width="stretch")


def item_label_html(item: dict) -> str:
    done = " done" if item["checked"] else ""
    name = html.escape(item["name"])
    qty = int(item["quantity"])
    return (
        f'<div class="item-line"><span class="item-name{done}">{name}</span>'
        f'<span class="qty-badge{done}">x{qty}</span></div>'
    )


def render_edit_row(item: dict) -> None:
    item_id = item["id"]
    # 편집 위젯이 화면에서 잠시 사라졌다 돌아와도 현재 값으로 다시 채움
    if f"edit_name_{item_id}" not in st.session_state or \
            f"edit_qty_{item_id}" not in st.session_state:
        _seed_edit_fields(item)
    col_name, col_qty = st.columns([3, 1.6], vertical_alignment="bottom")
    col_name.text_input("이름 수정", key=f"edit_name_{item_id}")
    col_qty.number_input("수량", min_value=QTY_MIN, max_value=QTY_MAX, step=1,
                         key=f"edit_qty_{item_id}", help=f"{QTY_MIN}~{QTY_MAX} 사이 숫자")
    if st.session_state["edit_error"] == item_id:
        st.warning("이름은 비워둘 수 없습니다.")
    col_save, col_cancel = st.columns(2)
    col_save.button("저장", key=f"save_{item_id}", on_click=save_edit,
                    args=(item_id,), type="primary", width="stretch")
    col_cancel.button("취소", key=f"cancel_{item_id}", on_click=cancel_edit,
                      width="stretch")


def render_item_row(item: dict) -> None:
    item_id = item["id"]
    editing_id = st.session_state["editing_id"]

    with st.container(border=True, key=f"row_{item_id}"):
        if editing_id == item_id:
            render_edit_row(item)
            return

        col_chk, col_name, col_edit, col_del = st.columns(
            [1, 8, 1, 1], vertical_alignment="center"
        )
        # 다른 화면에서 바뀐 체크 상태만 체크박스에 반영한다.
        # (값이 같을 때도 매번 설정하면 빠른 연속 클릭 중 화면 값이 되돌려질 수 있음)
        chk_key = f"chk_{item_id}"
        if st.session_state.get(chk_key) != item["checked"]:
            st.session_state[chk_key] = item["checked"]
        col_chk.checkbox(
            f"{escape_md(item['name'])} 구매 완료",
            key=f"chk_{item_id}",
            on_change=on_toggle,
            args=(item_id,),
            label_visibility="collapsed",
        )
        col_name.markdown(item_label_html(item), unsafe_allow_html=True)
        col_edit.button("✏️", key=f"edit_{item_id}", on_click=start_edit, args=(item_id,),
                        help="수정", type="tertiary",
                        disabled=editing_id is not None and editing_id != item_id)
        col_del.button("🗑️", key=f"del_{item_id}", on_click=on_delete, args=(item_id,),
                       help="삭제", type="tertiary")


def render_item_list(filter_option: str) -> None:
    items = st.session_state["items"]
    if filter_option == FILTER_TODO:
        visible = [i for i in items if not i["checked"]]
    elif filter_option == FILTER_DONE:
        visible = [i for i in items if i["checked"]]
    else:
        visible = items

    if not items:
        st.info("아직 아이템이 없어요. 위에서 살 물건을 추가해보세요!", icon="🧺")
        return
    if not visible:
        msg = "모두 구매했어요! 🎉" if filter_option == FILTER_TODO else "표시할 아이템이 없습니다."
        st.info(msg)
        return

    for item in list(visible):
        render_item_row(item)


def render_footer() -> None:
    if not st.session_state["items"]:
        return
    has_checked = any(i["checked"] for i in st.session_state["items"])
    st.button("체크된 항목 모두 삭제", on_click=on_clear_checked, disabled=not has_checked,
              icon="🧹", width="stretch")


# ---------------------------------------------------------------------------
# 6) main
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="쇼핑리스트", page_icon="🛒", layout="centered")
    inject_css()
    init_state()
    if msg := st.session_state.pop("toast", None):
        st.toast(msg)
    render_header()
    if warning := st.session_state.pop("load_warning", None):
        st.warning(warning)
    render_input_form()
    filter_option = render_filter()
    render_undo_bar()
    render_item_list(filter_option)
    render_footer()


if __name__ == "__main__":
    main()
