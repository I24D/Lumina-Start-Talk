"""Lumina-owned CEFR curriculum and real-world conversation scenarios.

The structure is original code. External language-learning projects were used
only as product references; no AGPL course files or prompts are copied here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


CEFR_LEVELS: list[dict[str, Any]] = [
    {
        "id": "A1",
        "title": "Primer contacto",
        "canDo": "Presentarte, pedir información básica y resolver necesidades inmediatas.",
        "units": [
            {"id": "a1-foundations", "title": "Presentaciones y datos personales", "skill": "conversation", "objective": "Presentarte y hacer preguntas simples", "lessons": 4},
            {"id": "a1-daily-life", "title": "Rutinas y entorno", "skill": "vocabulary", "objective": "Hablar de horarios, familia y lugares", "lessons": 5},
            {"id": "a1-survival", "title": "Inglés de supervivencia", "skill": "listening", "objective": "Comprar, pedir comida y entender indicaciones", "lessons": 5},
        ],
    },
    {
        "id": "A2",
        "title": "Comunicación cotidiana",
        "canDo": "Participar en intercambios breves sobre temas familiares y experiencias.",
        "units": [
            {"id": "a2-past-plans", "title": "Pasado y planes", "skill": "grammar", "objective": "Contar experiencias y expresar planes", "lessons": 5},
            {"id": "a2-travel", "title": "Viajes sin estrés", "skill": "conversation", "objective": "Resolver hotel, aeropuerto y transporte", "lessons": 6},
            {"id": "a2-social", "title": "Conversaciones sociales", "skill": "pronunciation", "objective": "Mantener una charla breve y clara", "lessons": 5},
        ],
    },
    {
        "id": "B1",
        "title": "Independencia",
        "canDo": "Explicar opiniones, narrar situaciones y desenvolverte en trabajo o viajes.",
        "units": [
            {"id": "b1-opinions", "title": "Opiniones y argumentos", "skill": "conversation", "objective": "Explicar y justificar una opinión", "lessons": 6},
            {"id": "b1-work", "title": "Inglés profesional", "skill": "vocabulary", "objective": "Participar en reuniones y escribir mensajes claros", "lessons": 6},
            {"id": "b1-stories", "title": "Historias y experiencias", "skill": "grammar", "objective": "Narrar con tiempos verbales consistentes", "lessons": 5},
        ],
    },
    {
        "id": "B2",
        "title": "Fluidez funcional",
        "canDo": "Interactuar con soltura y comprender ideas complejas en tu especialidad.",
        "units": [
            {"id": "b2-debate", "title": "Debate y matices", "skill": "conversation", "objective": "Comparar puntos de vista con precisión", "lessons": 6},
            {"id": "b2-presentations", "title": "Presentaciones", "skill": "pronunciation", "objective": "Presentar ideas con ritmo y estructura", "lessons": 5},
            {"id": "b2-media", "title": "Noticias y contenido real", "skill": "listening", "objective": "Identificar intención, tono y detalles", "lessons": 6},
        ],
    },
    {
        "id": "C1",
        "title": "Dominio avanzado",
        "canDo": "Usar el idioma con flexibilidad académica, profesional y social.",
        "units": [
            {"id": "c1-nuance", "title": "Precisión y registro", "skill": "vocabulary", "objective": "Elegir expresiones naturales según el contexto", "lessons": 6},
            {"id": "c1-leadership", "title": "Liderazgo y negociación", "skill": "conversation", "objective": "Negociar y manejar desacuerdos", "lessons": 6},
            {"id": "c1-analysis", "title": "Análisis crítico", "skill": "grammar", "objective": "Sintetizar y evaluar argumentos complejos", "lessons": 6},
        ],
    },
    {
        "id": "C2",
        "title": "Maestría",
        "canDo": "Comprender prácticamente todo y expresarte con precisión espontánea.",
        "units": [
            {"id": "c2-rhetoric", "title": "Retórica y estilo", "skill": "conversation", "objective": "Adaptar tono, humor y persuasión", "lessons": 6},
            {"id": "c2-specialist", "title": "Comunicación especializada", "skill": "vocabulary", "objective": "Dominar lenguaje técnico y abstracto", "lessons": 6},
            {"id": "c2-authentic", "title": "Comprensión auténtica", "skill": "listening", "objective": "Captar implicaciones, acentos y referencias", "lessons": 6},
        ],
    },
]


SCENARIOS: list[dict[str, Any]] = [
    {"id": "coffee-shop", "title": "Pedir en una cafetería", "category": "Vida diaria", "minLevel": "A1", "mode": "conversation", "duration": 6, "icon": "☕", "objective": "Pedir, aclarar y pagar con cortesía", "roles": {"student": "cliente", "tutor": "barista"}, "starter": "Good morning! What can I get for you?"},
    {"id": "directions", "title": "Pedir indicaciones", "category": "Viajes", "minLevel": "A1", "mode": "listening", "duration": 7, "icon": "🧭", "objective": "Entender giros, distancias y puntos de referencia", "roles": {"student": "viajero", "tutor": "persona local"}, "starter": "Excuse me, where are you trying to go?"},
    {"id": "hotel-checkin", "title": "Registro en un hotel", "category": "Viajes", "minLevel": "A2", "mode": "conversation", "duration": 8, "icon": "🏨", "objective": "Confirmar una reserva y resolver un problema", "roles": {"student": "huésped", "tutor": "recepcionista"}, "starter": "Welcome. Do you have a reservation with us?"},
    {"id": "doctor-visit", "title": "Visita al médico", "category": "Vida diaria", "minLevel": "A2", "mode": "vocabulary", "duration": 8, "icon": "🩺", "objective": "Describir síntomas y comprender recomendaciones", "roles": {"student": "paciente", "tutor": "profesional de salud"}, "starter": "What brings you in today?"},
    {"id": "job-interview", "title": "Entrevista de trabajo", "category": "Trabajo", "minLevel": "B1", "mode": "conversation", "duration": 10, "icon": "💼", "objective": "Explicar experiencia, fortalezas y objetivos", "roles": {"student": "candidato", "tutor": "entrevistador"}, "starter": "Tell me a little about yourself and your experience."},
    {"id": "team-meeting", "title": "Reunión de equipo", "category": "Trabajo", "minLevel": "B1", "mode": "listening", "duration": 10, "icon": "👥", "objective": "Dar avances, pedir aclaraciones y acordar acciones", "roles": {"student": "miembro del equipo", "tutor": "líder de reunión"}, "starter": "Let's begin with a quick update. What have you completed?"},
    {"id": "presentation", "title": "Presentación profesional", "category": "Trabajo", "minLevel": "B2", "mode": "pronunciation", "duration": 12, "icon": "📊", "objective": "Presentar con estructura, énfasis y ritmo natural", "roles": {"student": "presentador", "tutor": "coach"}, "starter": "Start with your opening statement. I will listen for clarity and impact."},
    {"id": "friendly-debate", "title": "Debate amistoso", "category": "Social", "minLevel": "B2", "mode": "conversation", "duration": 10, "icon": "💬", "objective": "Defender una opinión y responder con matices", "roles": {"student": "participante", "tutor": "interlocutor"}, "starter": "Do you think remote work improves people's lives? Why?"},
    {"id": "academic-discussion", "title": "Discusión académica", "category": "Estudio", "minLevel": "C1", "mode": "grammar", "duration": 12, "icon": "🎓", "objective": "Sintetizar evidencia y formular conclusiones prudentes", "roles": {"student": "estudiante", "tutor": "seminarista"}, "starter": "What is the strongest evidence for your position?"},
]


LISTENING_ACTIVITIES: list[dict[str, Any]] = [
    {
        "id": "missing-word",
        "title": "Completar el espacio",
        "icon": "▱",
        "instruction": "Di una frase breve ocultando una palabra clave. Repite el audio una vez si se solicita y pide completar solamente la palabra faltante.",
        "answerMode": "word",
    },
    {
        "id": "build-sentence",
        "title": "Construir la oración",
        "icon": "⌁",
        "instruction": "Di una oración natural una vez y pide al estudiante reconstruirla completa en el mismo orden.",
        "answerMode": "sentence",
    },
    {
        "id": "multiple-choice",
        "title": "Elegir la opción correcta",
        "icon": "◉",
        "instruction": "Lee un microdiálogo, ofrece tres interpretaciones breves y pide elegir una. No muestres la transcripción hasta responder.",
        "answerMode": "choice",
    },
    {
        "id": "spot-the-word",
        "title": "Identificar palabras",
        "icon": "⌕",
        "instruction": "Di una frase al nivel del estudiante y pide identificar dos palabras concretas o expresiones que escuchó.",
        "answerMode": "words",
    },
]


def _level_rank(level: str) -> int:
    levels = [item["id"] for item in CEFR_LEVELS]
    try:
        return levels.index(str(level or "A1").upper())
    except ValueError:
        return 0


def find_scenario(scenario_id: str) -> dict[str, Any] | None:
    found = next((item for item in SCENARIOS if item["id"] == scenario_id), None)
    return deepcopy(found) if found else None


def find_unit(unit_id: str) -> dict[str, Any] | None:
    for level in CEFR_LEVELS:
        for unit in level["units"]:
            if unit["id"] == unit_id:
                return {**deepcopy(unit), "level": level["id"]}
    return None


def find_listening_activity(activity_id: str) -> dict[str, Any] | None:
    found = next(
        (item for item in LISTENING_ACTIVITIES if item["id"] == activity_id), None
    )
    return deepcopy(found) if found else None


def build_catalog(profile: dict[str, Any], curriculum: dict[str, Any]) -> dict[str, Any]:
    level = str(profile.get("level") or "A1").upper()
    rank = _level_rank(level)
    goal = str(profile.get("goal") or "").casefold()
    preferred = "job-interview" if any(x in goal for x in ("trabaj", "work", "business")) else "hotel-checkin" if any(x in goal for x in ("viaj", "travel")) else "coffee-shop"
    scenarios = deepcopy(SCENARIOS)
    for item in scenarios:
        item["available"] = _level_rank(item["minLevel"]) <= rank + 1
        item["recommended"] = item["id"] == preferred
    completed = set(curriculum.get("completed_units") or [])
    levels = deepcopy(CEFR_LEVELS)
    for level_item in levels:
        for unit in level_item["units"]:
            unit["completed"] = unit["id"] in completed
            unit["current"] = unit["id"] == curriculum.get("current_unit_id")
            unit["available"] = _level_rank(level_item["id"]) <= rank
    return {
        "scenarios": scenarios,
        "levels": levels,
        "listeningActivities": deepcopy(LISTENING_ACTIVITIES),
        "recommendedScenarioId": preferred,
        "currentLevel": level,
    }
