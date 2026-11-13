"""Explanation layer: the model writes one short `explanation` from the rule facts and picks
the primary action among the preset candidates. It never changes the level, the findings,
the limitation or the action texts.

Validation (any failure = invalid): JSON with exactly the expected keys, explanation length
<= MAINTENANCE_NARRATOR_MAX_CHARS, every number / time / date backed by the evidence (a
rounding that keeps >= 2 significant digits is accepted), primary_action among the candidates.
Invalid -> one retry with the error -> rule template (explanation_source = "template").
insufficient_data never calls the model.
"""

import asyncio
import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

SYSTEM = """你是核电站固定监测站的预测性维护解释助手。你会收到规则引擎已经判定的事实（JSON）。

要求：
1. 只根据给定事实写一段简短的综合判断（explanation），说明判断依据和优先处理方向。
2. 健康等级用 health_level_label 的中文表述，不得改变健康等级，不得新增事实中没有的数值、时间、日期、设备或原因；数值可保留更少位小数。
3. 在 candidate_actions 中选择一个最应优先执行的动作，填写其 action_code 作为 primary_action；不得编造或改写动作。
4. 不写设备操作或控制指令；结论仅供授权人员人工核查。
5. 使用简洁、专业的中文，explanation 不超过 {max_chars} 个字符。
6. 只输出一个 JSON 对象：{{"explanation": "...", "primary_action": "..."}}"""

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
# a number not glued to an identifier ("AS01", "DET-01", "H1"); a minus sign is allowed and
# ignored, so "-25" in the evidence supports "下降 25%"
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(?<![A-Za-z0-9_]-)\d+(?:\.\d+)?(?![A-Za-z0-9_])")


class InvalidNarration(ValueError):
    pass


def _hhmm(t: str) -> str:
    h, m = t.split(":")[:2]
    return f"{int(h):02d}:{m}"


def numbers(text: str) -> list[str]:
    return _NUMBER.findall(_TIME.sub(" ", _DATE.sub(" ", text)))


def _sig_digits(token: str) -> int:
    return len(token.replace(".", "").lstrip("0"))


def supported(token: str, allowed: list[float]) -> bool:
    value = float(token)
    decimals = len(token.split(".")[1]) if "." in token else 0
    for v in allowed:
        if abs(v - value) <= 1e-9:
            return True
        if _sig_digits(token) >= 2 and round(v, decimals) == value and \
                (decimals > 0 or abs(v) >= 10):
            return True
    return False


def check_text(text: str, evidence: dict) -> None:
    """Raise InvalidNarration if `text` contains a number, time or date not in `evidence`."""
    source = json.dumps(evidence, ensure_ascii=False)
    dates = set(_DATE.findall(source))
    times = {_hhmm(t) for t in _TIME.findall(source)} | {"00:00", "24:00"}
    allowed = [float(n) for n in numbers(source)]
    for d in _DATE.findall(text):
        if d not in dates:
            raise InvalidNarration(f"日期 {d} 不在事实中")
    for t in _TIME.findall(text):
        if _hhmm(t) not in times:
            raise InvalidNarration(f"时间 {t} 不在事实中")
    for n in numbers(text):
        if not supported(n, allowed):
            raise InvalidNarration(f"数值 {n} 无事实支持")


def parse(raw: str, evidence: dict, candidates: list[str], max_chars: int) -> tuple[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidNarration("输出不是 JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("explanation"), str):
        raise InvalidNarration("缺少 explanation 字段")
    explanation = data["explanation"].strip()
    primary = str(data.get("primary_action", "")).strip()
    if not explanation:
        raise InvalidNarration("explanation 为空")
    if len(explanation) > max_chars:
        raise InvalidNarration(f"explanation 超过 {max_chars} 个字符")
    if primary not in candidates:
        raise InvalidNarration(f"primary_action {primary!r} 不在候选动作中")
    check_text(explanation, evidence)
    return explanation, primary


class Narrator:
    def __init__(self, llm, max_chars: int = 300, timeout: float = 30) -> None:
        self.llm = llm.bind(response_format={"type": "json_object"}) if llm is not None else None
        self.max_chars, self.timeout = max_chars, timeout

    async def narrate(self, evidence: dict, candidates: list[str]
                      ) -> tuple[str, str, str | None]:
        """(explanation, primary_action, error). Raises InvalidNarration when the model is
        unavailable or both attempts are invalid; the caller then uses the template."""
        if self.llm is None:
            raise InvalidNarration("未配置解释模型")
        messages = [SystemMessage(SYSTEM.format(max_chars=self.max_chars)),
                    HumanMessage("规则事实：\n```json\n"
                                 + json.dumps(evidence, ensure_ascii=False, indent=1) + "\n```")]
        error = "未知错误"
        for attempt in range(2):
            try:
                reply = await asyncio.wait_for(self.llm.ainvoke(messages), self.timeout)
                raw = reply.content if isinstance(reply.content, str) else str(reply.content)
                explanation, primary = parse(raw, evidence, candidates, self.max_chars)
                return explanation, primary, None
            except InvalidNarration as exc:
                error = str(exc)
                logger.info("narration rejected (attempt %d): %s", attempt + 1, error)
                messages = [*messages, HumanMessage(
                    f"上一次输出无效：{error}。请严格依据规则事实重新输出 JSON。")]
            except Exception as exc:  # timeout, connection ...
                raise InvalidNarration(f"解释模型调用失败：{type(exc).__name__}") from exc
        raise InvalidNarration(error)
