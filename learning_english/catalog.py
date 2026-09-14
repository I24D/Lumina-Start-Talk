"""Lumina-owned CEFR curriculum and real-world conversation scenarios.

The structure is original code. External language-learning projects were used
only as product references; no AGPL course files or prompts are copied here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


# Who a class is for. The tutor adapts tone, topics and pace to it.
AUDIENCES = ("kids", "teens", "adults")


CEFR_LEVELS: list[dict[str, Any]] = [
    {
        "id": "PRE-A1",
        "title": "Desde cero",
        "canDo": "Reconocer sonidos, saludar, contar y nombrar cosas cotidianas con apoyo en tu idioma.",
        "units": [
            {"id": "pre-a1-sounds", "title": "El alfabeto y sus sonidos", "skill": "pronunciation", "objective": "Reconocer y decir las letras y los sonidos del inglés", "lessons": 4},
            {"id": "pre-a1-first-words", "title": "Números, colores y objetos", "skill": "vocabulary", "objective": "Contar hasta 20 y nombrar colores y objetos de casa", "lessons": 5},
            {"id": "pre-a1-greetings", "title": "Hola, adiós y por favor", "skill": "conversation", "objective": "Saludar, despedirte y usar palabras de cortesía", "lessons": 4},
        ],
    },
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
    {"id": "classroom", "title": "En el salón de clases", "category": "Escuela", "minLevel": "PRE-A1", "mode": "vocabulary", "duration": 5, "icon": "🎒", "objective": "Nombrar útiles escolares y seguir instrucciones sencillas", "roles": {"student": "alumno", "tutor": "maestra de la clase"}, "starter": "Hello! Can you show me your pencil?", "audiences": ["kids"]},
    {"id": "pets", "title": "Mis mascotas y animales", "category": "Mi mundo", "minLevel": "PRE-A1", "mode": "vocabulary", "duration": 5, "icon": "🐶", "objective": "Nombrar animales, sus colores y lo que hacen", "roles": {"student": "visitante de la granja", "tutor": "cuidadora de animales"}, "starter": "Look! A dog! What color is the dog?", "audiences": ["kids"]},
    {"id": "birthday-party", "title": "Fiesta de cumpleaños", "category": "Mi mundo", "minLevel": "A1", "mode": "conversation", "duration": 6, "icon": "🎂", "objective": "Felicitar, decir tu edad y hablar de regalos", "roles": {"student": "invitado", "tutor": "amiga que cumple años"}, "starter": "Hi! Welcome to my party! How old are you?", "audiences": ["kids"]},
    {"id": "playground", "title": "Juegos en el parque", "category": "Mi mundo", "minLevel": "A1", "mode": "listening", "duration": 6, "icon": "🛝", "objective": "Invitar a jugar y entender reglas sencillas", "roles": {"student": "niño en el parque", "tutor": "nueva amiga"}, "starter": "Hi! Do you want to play with me?", "audiences": ["kids"]},
    {"id": "coffee-shop", "title": "Pedir en una cafetería", "category": "Vida diaria", "minLevel": "A1", "mode": "conversation", "duration": 6, "icon": "☕", "objective": "Pedir, aclarar y pagar con cortesía", "roles": {"student": "cliente", "tutor": "barista"}, "starter": "Good morning! What can I get for you?", "audiences": ["teens", "adults"]},
    {"id": "directions", "title": "Pedir indicaciones", "category": "Viajes", "minLevel": "A1", "mode": "listening", "duration": 7, "icon": "🧭", "objective": "Entender giros, distancias y puntos de referencia", "roles": {"student": "viajero", "tutor": "persona local"}, "starter": "Excuse me, where are you trying to go?", "audiences": ["teens", "adults"]},
    {"id": "gaming-online", "title": "Jugar en línea con amigos", "category": "Social", "minLevel": "A2", "mode": "conversation", "duration": 8, "icon": "🎮", "objective": "Coordinar una partida y comunicarte en equipo", "roles": {"student": "jugador", "tutor": "compañera de equipo"}, "starter": "Hey, are you ready? Which character are you going to play?", "audiences": ["teens"]},
    {"id": "school-project", "title": "Proyecto escolar en equipo", "category": "Estudio", "minLevel": "A2", "mode": "conversation", "duration": 8, "icon": "🧪", "objective": "Repartir tareas y acordar fechas", "roles": {"student": "estudiante", "tutor": "compañera de equipo"}, "starter": "Okay, our science project is due on Friday. Which part do you want to do?", "audiences": ["teens"]},
    {"id": "hotel-checkin", "title": "Registro en un hotel", "category": "Viajes", "minLevel": "A2", "mode": "conversation", "duration": 8, "icon": "🏨", "objective": "Confirmar una reserva y resolver un problema", "roles": {"student": "huésped", "tutor": "recepcionista"}, "starter": "Welcome. Do you have a reservation with us?", "audiences": ["adults"]},
    {"id": "doctor-visit", "title": "Visita al médico", "category": "Vida diaria", "minLevel": "A2", "mode": "vocabulary", "duration": 8, "icon": "🩺", "objective": "Describir síntomas y comprender recomendaciones", "roles": {"student": "paciente", "tutor": "profesional de salud"}, "starter": "What brings you in today?", "audiences": ["teens", "adults"]},
    {"id": "speaking-exam", "title": "Examen oral de inglés", "category": "Estudio", "minLevel": "B1", "mode": "conversation", "duration": 10, "icon": "📝", "objective": "Responder con estructura y ejemplos a tiempo", "roles": {"student": "candidato", "tutor": "examinadora"}, "starter": "Good afternoon. Can you tell me about a place you like to visit?", "audiences": ["teens", "adults"]},
    {"id": "job-interview", "title": "Entrevista de trabajo", "category": "Trabajo", "minLevel": "B1", "mode": "conversation", "duration": 10, "icon": "💼", "objective": "Explicar experiencia, fortalezas y objetivos", "roles": {"student": "candidato", "tutor": "entrevistador"}, "starter": "Tell me a little about yourself and your experience.", "audiences": ["adults"]},
    {"id": "team-meeting", "title": "Reunión de equipo", "category": "Trabajo", "minLevel": "B1", "mode": "listening", "duration": 10, "icon": "👥", "objective": "Dar avances, pedir aclaraciones y acordar acciones", "roles": {"student": "miembro del equipo", "tutor": "líder de reunión"}, "starter": "Let's begin with a quick update. What have you completed?", "audiences": ["adults"]},
    {"id": "presentation", "title": "Presentación profesional", "category": "Trabajo", "minLevel": "B2", "mode": "pronunciation", "duration": 12, "icon": "📊", "objective": "Presentar con estructura, énfasis y ritmo natural", "roles": {"student": "presentador", "tutor": "coach"}, "starter": "Start with your opening statement. I will listen for clarity and impact.", "audiences": ["teens", "adults"]},
    {"id": "friendly-debate", "title": "Debate amistoso", "category": "Social", "minLevel": "B2", "mode": "conversation", "duration": 10, "icon": "💬", "objective": "Defender una opinión y responder con matices", "roles": {"student": "participante", "tutor": "interlocutor"}, "starter": "Do you think remote work improves people's lives? Why?", "audiences": ["teens", "adults"]},
    {"id": "academic-discussion", "title": "Discusión académica", "category": "Estudio", "minLevel": "C1", "mode": "grammar", "duration": 12, "icon": "🎓", "objective": "Sintetizar evidencia y formular conclusiones prudentes", "roles": {"student": "estudiante", "tutor": "seminarista"}, "starter": "What is the strongest evidence for your position?", "audiences": ["teens", "adults"]},
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


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def level_label(level: str) -> str:
    level = str(level or "A1").upper()
    return "Pre-A1" if level == "PRE-A1" else level


def audience_of(profile: dict[str, Any] | None) -> str:
    """The saved audience, or adults when the student has not said."""
    audience = str((profile or {}).get("audience") or "").lower()
    return audience if audience in AUDIENCES else "adults"


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


def next_unit_id(level: str, completed: Any) -> str:
    """The first unfinished unit of the student's level, in route order.

    Empty once that level's units are all done: the next level opens only
    through the level assessment, never by finishing lessons alone."""
    done = set(completed or [])
    wanted = str(level or "A1").upper()
    for level_item in CEFR_LEVELS:
        if level_item["id"] == wanted:
            return next(
                (unit["id"] for unit in level_item["units"] if unit["id"] not in done), ""
            )
    return ""


def _recommended_scenario(audience: str, rank: int, goal: str) -> str:
    if audience == "kids":
        return ("classroom", "birthday-party")[rank] if rank < 2 else "playground"
    if audience == "teens":
        return "gaming-online" if rank >= 1 else "coffee-shop"
    if any(x in goal for x in ("trabaj", "work", "business")):
        return "job-interview"
    if any(x in goal for x in ("viaj", "travel")):
        return "hotel-checkin"
    return "coffee-shop"


def build_catalog(profile: dict[str, Any], curriculum: dict[str, Any]) -> dict[str, Any]:
    level = str(profile.get("level") or "A1").upper()
    rank = _level_rank(level)
    audience = audience_of(profile)
    goal = str(profile.get("goal") or "").casefold()
    preferred = _recommended_scenario(audience, rank, goal)
    finished_scenarios = set(curriculum.get("completed_scenarios") or [])
    scenarios = deepcopy(SCENARIOS)
    for item in scenarios:
        item["available"] = _level_rank(item["minLevel"]) <= rank + 1
        item["recommended"] = item["id"] == preferred
        item["completed"] = item["id"] in finished_scenarios
        item["forAudience"] = audience in item["audiences"]
    # The student's own age group first; the rest stay reachable below it.
    scenarios.sort(key=lambda item: (not item["forAudience"], _level_rank(item["minLevel"])))
    completed = set(curriculum.get("completed_units") or [])
    lessons_done = curriculum.get("unit_lessons")
    if not isinstance(lessons_done, dict):
        lessons_done = {}
    levels = deepcopy(CEFR_LEVELS)
    for level_item in levels:
        for unit in level_item["units"]:
            unit["completed"] = unit["id"] in completed
            unit["current"] = unit["id"] == curriculum.get("current_unit_id")
            unit["available"] = _level_rank(level_item["id"]) <= rank
            unit["lessonsDone"] = min(unit["lessons"], _count(lessons_done.get(unit["id"])))
    return {
        "scenarios": scenarios,
        "levels": levels,
        "listeningActivities": deepcopy(LISTENING_ACTIVITIES),
        "recommendedScenarioId": preferred,
        "currentLevel": level,
        "audience": audience,
        "levelComplete": not next_unit_id(level, completed),
    }


def next_step(
    profile: dict[str, Any], catalog: dict[str, Any], *, reviews_due: int
) -> dict[str, str]:
    """The one thing the studio recommends now, decided by the course, not by Gemini."""
    if not profile.get("level_confirmed"):
        return {
            "kind": "placement",
            "title": "Descubre tu nivel",
            "detail": "Una prueba corta que se adapta a tus respuestas, aunque no sepas nada de inglés.",
        }
    if reviews_due >= 3:
        return {
            "kind": "review",
            "title": f"Repasa {reviews_due} palabras",
            "detail": "Es el mejor momento para recordarlas antes de olvidarlas.",
        }
    level = catalog.get("currentLevel", "A1")
    if catalog.get("levelComplete"):
        return {
            "kind": "assessment",
            "title": f"Terminaste {level_label(level)}",
            "detail": "Haz la evaluación de nivel con tu maestra para pasar al siguiente.",
        }
    unit = next(
        (u for item in catalog.get("levels", []) for u in item["units"] if u.get("current")),
        None,
    )
    if unit:
        return {
            "kind": "unit",
            "title": unit["title"],
            "detail": f"{unit['lessonsDone']}/{unit['lessons']} lecciones · {unit['objective']}",
        }
    return {
        "kind": "conversation",
        "title": "Conversa con tu maestra",
        "detail": "Practica lo que quieras hoy.",
    }
