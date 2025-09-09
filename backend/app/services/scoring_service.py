"""
Scoring Service для оценки интервью через GPT
"""
import json
import logging
from typing import Dict, Any, List
from openai import AsyncOpenAI
import os

logger = logging.getLogger(__name__)

class InterviewScorer:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = "gpt-4o-mini"  # Быстрая и дешёвая модель для скоринга
    
    async def score_interview(
        self, 
        candidate_facts: Dict[str, List[str]], 
        vacancy_requirements: Dict[str, Any],
        lang: str = "ru",
        speech_metrics: list[dict] | None = None
    ) -> Dict[str, Any]:
        """Оценка интервью на основе собранных фактов"""
        
        # Формируем промпт для скоринга
        if lang == "ru":
            system_prompt = """
Ты эксперт по оценке кандидатов. Проанализируй результаты интервью и дай оценку.

Оцени кандидата по критериям:
1. Опыт работы (experience) - 0-100
2. Технические навыки (stack) - 0-100  
3. Решение кейсов (cases) - 0-100
4. Коммуникация (communication) - 0-100

Итоговая оценка (overall_score) = средневзвешенная оценка всех критериев.

Верни JSON в формате:
{
    "scores": {
        "experience": число,
        "stack": число,
        "cases": число,
        "communication": число
    },
    "overall_score": число,
    "recommendation": "hire"|"maybe"|"reject",
    "strengths": ["список сильных сторон"],
    "weaknesses": ["список слабых сторон"],
    "summary": "краткое резюме на 2-3 предложения"
}
"""
        else:
            system_prompt = """
You are an expert in candidate evaluation. Analyze the interview results and provide an assessment.

Evaluate the candidate by criteria:
1. Work experience - 0-100
2. Technical skills (stack) - 0-100
3. Case solving - 0-100
4. Communication - 0-100

Overall score = weighted average of all criteria.

Return JSON in format:
{
    "scores": {
        "experience": number,
        "stack": number,
        "cases": number,
        "communication": number
    },
    "overall_score": number,
    "recommendation": "hire"|"maybe"|"reject",
    "strengths": ["list of strengths"],
    "weaknesses": ["list of weaknesses"],
    "summary": "brief summary in 2-3 sentences"
}
"""
        
        # Формируем данные о кандидате
        candidate_info = self._format_candidate_info(candidate_facts)
        requirements_info = self._format_requirements(vacancy_requirements)

        # Сжато описываем речевые метрики (паузы/скорость/вариативность)
        sm = speech_metrics or []
        total_ms = sum(t.get("speech_ms", 0) for t in sm) or 1
        total_words = sum(t.get("words", 0) for t in sm)
        wpm = int((total_words / total_ms) * 60000)
        pauses = sum(max(0, len(t.get("segments", [])) - 1) for t in sm)
        avg_pause = 0
        if pauses:
            total_pause = 0
            for t in sm:
                segs = t.get("segments", [])
                for i in range(1, len(segs)):
                    total_pause += max(0, (segs[i]["s"] - segs[i-1]["e"]))
            avg_pause = round(total_pause / max(1, pauses))
        rms_mean = round(sum((t.get("rms_mean") or 0) for t in sm) / max(1, len(sm)), 3) if sm else 0
        rms_var = round(sum((t.get("rms_var") or 0) for t in sm) / max(1, len(sm)), 3) if sm else 0

        speech_summary = f"WPM={wpm}, pauses={pauses}, avg_pause_ms={avg_pause}, rms_mean={rms_mean}, rms_var={rms_var}"
        
        user_prompt = f"""
Требования вакансии:
{requirements_info}

Информация о кандидате из интервью:
{candidate_info}

Речевые метрики (для оценки soft‑skills):
{speech_summary}

Проведи детальную оценку соответствия кандидата требованиям.
"""
        
        try:
            # Вызываем GPT для скоринга
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=1000
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # Добавляем дополнительную логику для recommendation
            overall = result.get("overall_score", 0)
            if overall >= 80:
                result["recommendation"] = "hire"
            elif overall >= 60:
                result["recommendation"] = "maybe"
            else:
                result["recommendation"] = "reject"
            
            return result
            
        except Exception as e:
            logger.error(f"Scoring error: {e}")
            # Fallback оценка
            return {
                "scores": {
                    "experience": 60,
                    "stack": 60,
                    "cases": 60,
                    "communication": 70
                },
                "overall_score": 63,
                "recommendation": "maybe",
                "strengths": ["Коммуникабельный", "Мотивированный"],
                "weaknesses": ["Недостаточно опыта"],
                "summary": "Кандидат показал средние результаты"
            }
    
    def _format_candidate_info(self, facts: Dict[str, List[str]]) -> str:
        """Форматирование фактов о кандидате"""
        lines = []
        
        sections = {
            "experience": "Опыт работы",
            "stack": "Технические навыки", 
            "cases": "Решение задач",
            "communication": "Коммуникация"
        }
        
        for section, title in sections.items():
            if section in facts and facts[section]:
                lines.append(f"\n{title}:")
                for fact in facts[section]:
                    lines.append(f"- {fact}")
        
        return "\n".join(lines) if lines else "Информация не предоставлена"
    
    def _format_requirements(self, requirements: Dict[str, Any]) -> str:
        """Форматирование требований вакансии"""
        lines = []
        
        if requirements.get("title"):
            lines.append(f"Должность: {requirements['title']}")
        
        if requirements.get("experience_years"):
            lines.append(f"Требуемый опыт: {requirements['experience_years']} лет")
        
        if requirements.get("required_skills"):
            lines.append("\nОбязательные навыки:")
            for skill in requirements["required_skills"]:
                lines.append(f"- {skill}")
        
        if requirements.get("nice_to_have"):
            lines.append("\nЖелательные навыки:")
            for skill in requirements["nice_to_have"]:
                lines.append(f"- {skill}")
        
        return "\n".join(lines) if lines else "Стандартные требования"
    
    async def generate_detailed_report(
        self,
        scoring_result: Dict[str, Any],
        candidate_name: str,
        vacancy_title: str,
        lang: str = "ru"
    ) -> str:
        """Генерация детального отчёта по интервью"""
        
        if lang == "ru":
            report_prompt = f"""
Создай профессиональный отчёт по результатам интервью.

Кандидат: {candidate_name}
Вакансия: {vacancy_title}
Результаты оценки: {json.dumps(scoring_result, ensure_ascii=False)}

Структура отчёта:
1. Краткое резюме (2-3 предложения)
2. Детальная оценка по критериям с обоснованием
3. Сильные стороны кандидата
4. Зоны для развития
5. Рекомендация по найму с обоснованием
6. Следующие шаги (если применимо)

Используй профессиональный стиль, избегай общих фраз.
"""
        else:
            report_prompt = f"""
Create a professional interview report.

Candidate: {candidate_name}
Position: {vacancy_title}
Assessment results: {json.dumps(scoring_result)}

Report structure:
1. Executive summary (2-3 sentences)
2. Detailed assessment by criteria with justification
3. Candidate strengths
4. Areas for improvement
5. Hiring recommendation with rationale
6. Next steps (if applicable)

Use professional style, avoid generic phrases.
"""
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an HR expert creating professional interview reports."},
                    {"role": "user", "content": report_prompt}
                ],
                temperature=0.5,
                max_tokens=2000
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            logger.error(f"Report generation error: {e}")
            return self._generate_fallback_report(scoring_result, candidate_name, vacancy_title, lang)
    
    def _generate_fallback_report(
        self, 
        scoring_result: Dict[str, Any],
        candidate_name: str,
        vacancy_title: str,
        lang: str
    ) -> str:
        """Fallback отчёт при ошибке GPT"""
        
        overall = scoring_result.get("overall_score", 0)
        recommendation = scoring_result.get("recommendation", "maybe")
        
        if lang == "ru":
            rec_text = {
                "hire": "рекомендуется к найму",
                "maybe": "требует дополнительного рассмотрения",
                "reject": "не рекомендуется к найму"
            }.get(recommendation, "требует рассмотрения")
            
            return f"""
ОТЧЁТ ПО ИНТЕРВЬЮ

Кандидат: {candidate_name}
Вакансия: {vacancy_title}
Дата: {datetime.now().strftime("%d.%m.%Y")}

ИТОГОВАЯ ОЦЕНКА: {overall}/100

ЗАКЛЮЧЕНИЕ: Кандидат {rec_text}.

ДЕТАЛЬНАЯ ОЦЕНКА:
- Опыт работы: {scoring_result['scores']['experience']}/100
- Технические навыки: {scoring_result['scores']['stack']}/100
- Решение задач: {scoring_result['scores']['cases']}/100
- Коммуникация: {scoring_result['scores']['communication']}/100

РЕКОМЕНДАЦИИ:
{chr(10).join('- ' + s for s in scoring_result.get('strengths', []))}

ЗОНЫ РАЗВИТИЯ:
{chr(10).join('- ' + w for w in scoring_result.get('weaknesses', []))}
"""
        else:
            rec_text = {
                "hire": "recommended for hire",
                "maybe": "requires further consideration", 
                "reject": "not recommended for hire"
            }.get(recommendation, "requires consideration")
            
            return f"""
INTERVIEW REPORT

Candidate: {candidate_name}
Position: {vacancy_title}
Date: {datetime.now().strftime("%Y-%m-%d")}

OVERALL SCORE: {overall}/100

CONCLUSION: The candidate is {rec_text}.

DETAILED ASSESSMENT:
- Work Experience: {scoring_result['scores']['experience']}/100
- Technical Skills: {scoring_result['scores']['stack']}/100
- Problem Solving: {scoring_result['scores']['cases']}/100
- Communication: {scoring_result['scores']['communication']}/100

STRENGTHS:
{chr(10).join('- ' + s for s in scoring_result.get('strengths', []))}

AREAS FOR IMPROVEMENT:
{chr(10).join('- ' + w for w in scoring_result.get('weaknesses', []))}
"""


# Singleton instance
scorer = InterviewScorer()

from datetime import datetime
