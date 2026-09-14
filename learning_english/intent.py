"""Deterministic bilingual intent detector for entering and leaving lessons."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IntentMatch:
    action: str = "none"
    confidence: float = 0.0
    reason: str = ""


def _normalise(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())


_ENTER_STRONG = (
    r"\b(?:quiero|deseo) (?:aprender|estudiar|practicar) ingles\b",
    r"\b(?:ensename|ayudame (?:a|con)) (?:el |mi )?ingles\b",
    r"\b(?:activa|abre|inicia|empieza) (?:el modo )?(?:learning english|lumina learning)\b",
    r"\b(?:quiero|dame) una clase de ingles\b",
    r"\b(?:practiquemos|quiero practicar) pronunciacion\b",
    r"\bteach me english\b",
    r"\b(?:i want to|let s|lets) practice english\b",
    r"\bi want to learn english\b",
)
# Leaving must work however the student words it: a missed exit strands them in
# a tutor session that has no assistant tools. Stopping the lesson and returning
# to the assistant are separate verb groups, so "vuelve a la clase" never exits.
_STOP_VERBS = (
    r"desactiva(?:r|lo|la)?|apaga(?:r|lo|la)?|cierra(?:lo|la)?|cerrar|sal|salir|salte|salgamos"
    r"|termina(?:r|lo|la)?|terminemos|finaliza(?:r|lo|la)?|acaba(?:r|lo|la)?"
    r"|quita(?:r|lo|la)?|deten|detener"
)
_RETURN_VERBS = r"vuelve|volver|volvamos|regresa|regresar|regresemos"
# No "en" here: "cómo se dice sal en inglés" is a lesson question, not an exit.
_FILLERS = r"(?: (?:el|la|lo|los|las|del|al|a|de|mi|este|esta|ese|esa|modo|conversacion|ya|ahora))*"
_LESSON_TARGETS = (
    r"learning|lerning|ingles|english|aprendizaje|clase|leccion|estudio|tutor|maestra|profesora"
)
_ASSISTANT_TARGETS = r"lumina|asistente|normal|general"

_EXIT_STRONG = (
    rf"\b(?:{_STOP_VERBS}){_FILLERS} (?:{_LESSON_TARGETS})\b",
    rf"\b(?:{_RETURN_VERBS}|cambia|cambiar|pasa|pasar){_FILLERS} (?:{_ASSISTANT_TARGETS})\b",
    r"\b(?:exit|leave|quit|stop|end|close|disable|deactivate|turn off|switch off) "
    r"(?:the )?(?:english |learning )?(?:lesson|class|mode)\b",
    r"\b(?:exit|leave|quit|disable|deactivate|turn off|switch off) (?:the )?learning english\b",
    r"\b(?:go )?back to (?:lumina|normal|the (?:general )?assistant)\b",
    r"^(?:lumina )?(?:modo (?:normal|general|asistente(?: general)?)|(?:normal|general|assistant) mode)$",
    # A bare "sí, termina" answering the tutor's own offer to end the class.
    rf"^(?:(?:si|ok|okay|vale|ya|claro|dale|listo|bueno|lumina) )*(?:{_STOP_VERBS}|{_RETURN_VERBS})"
    r"(?: (?:ya|ahora|porfa|por favor))*$",
)


def detect_learning_english_intent(text: str, *, active: bool = False) -> IntentMatch:
    """Return a conservative local intent result.

    Explicit phrases are high confidence and can switch immediately. A vague
    mention of learning plus English is medium confidence so the caller can ask
    first. Plain mentions such as "English weather" remain normal conversation.
    """
    value = _normalise(text)
    if not value:
        return IntentMatch()

    if active and any(re.search(pattern, value) for pattern in _EXIT_STRONG):
        return IntentMatch("exit", 0.99, "explicit exit phrase")
    if any(re.search(pattern, value) for pattern in _ENTER_STRONG):
        return IntentMatch("enter", 0.98, "explicit learning phrase")

    english = bool(re.search(r"\b(?:ingles|english)\b", value))
    learning = bool(re.search(r"\b(?:aprender|estudiar|practicar|clase|learn|study|practice)\b", value))
    if not active and english and learning:
        return IntentMatch("enter", 0.68, "possible learning request")
    if active and re.search(r"\b(?:salir|terminar|volver|regresar|exit|leave|back)\b", value):
        return IntentMatch("exit", 0.62, "possible exit request")
    return IntentMatch()


def confirmation_answer(text: str) -> bool | None:
    value = _normalise(text)
    if re.fullmatch(r"(?:si|claro|correcto|adelante|hazlo|yes|sure|please do)", value):
        return True
    if re.fullmatch(r"(?:no|no gracias|cancelar|cancela|mejor no|not now|no thanks)", value):
        return False
    return None
