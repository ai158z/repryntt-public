"""
repryntt.routing.task_hierarchy — Task-type classification.

Classifies incoming prompts into task types (research, creative, technical, …)
so the routing layer can select appropriate tools, chain phases, and parameters.

Extracted from: SAIGE/brain/brain_system.py lines 152-644
    TaskType, TaskConfiguration, TaskHierarchySystem
"""

import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TaskType(Enum):
    RESEARCH_ANALYSIS = "research_analysis"
    CREATIVE_WRITING = "creative_writing"
    STRATEGIC_PLANNING = "strategic_planning"
    PROBLEM_SOLVING = "problem_solving"
    LEARNING_EDUCATION = "learning_education"
    TECHNICAL_DEVELOPMENT = "technical_development"
    TECHNICAL_INNOVATION = "technical_innovation"
    BUSINESS_DEVELOPMENT = "business_development"
    UNKNOWN = "unknown"


@dataclass
class TaskConfiguration:
    task_type: TaskType
    name: str
    description: str
    recognition_keywords: List[str]
    primary_goal: str
    evaluation_criteria: List[str]
    preferred_tools: List[str]
    chain_phases: List[str]
    prompt_templates: Dict[str, str]
    success_metrics: List[str]
    priority_tools: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["task_type"] = self.task_type.value
        return data


class TaskHierarchySystem:
    """Keyword-based task classifier with per-type tool/phase configs."""

    def __init__(self):
        self.task_configs = self._build_configs()
        self.task_history: deque = deque(maxlen=100)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify_task(
        self,
        description: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> TaskConfiguration:
        clean = description.lower().strip()
        scores: Dict[TaskType, int] = {}
        for tt, cfg in self.task_configs.items():
            score = 0
            for kw in cfg.recognition_keywords:
                if kw.lower() in clean:
                    score += 1
                if f" {kw.lower()} " in f" {clean} ":
                    score += 2
            scores[tt] = score

        best_type, best_score = max(scores.items(), key=lambda x: x[1])
        if best_score < 2:
            return self.task_configs[TaskType.UNKNOWN]

        selected = self.task_configs[best_type]
        logger.info(f"Task classified: {selected.name} (score={best_score})")
        self.task_history.append({
            "description": description,
            "type": best_type.value,
            "score": best_score,
            "ts": time.time(),
        })
        return selected

    def get_task_config(self, task_type: TaskType) -> TaskConfiguration:
        return self.task_configs.get(
            task_type, self.task_configs[TaskType.RESEARCH_ANALYSIS]
        )

    def get_available_task_types(self) -> List[str]:
        return [c.name for c in self.task_configs.values()]

    def get_task_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        return list(self.task_history)[-limit:]

    # ------------------------------------------------------------------
    # Config definitions
    # ------------------------------------------------------------------

    @staticmethod
    def _build_configs() -> Dict[TaskType, TaskConfiguration]:
        return {
            TaskType.RESEARCH_ANALYSIS: TaskConfiguration(
                task_type=TaskType.RESEARCH_ANALYSIS,
                name="Research & Analysis",
                description="Deep investigation, data analysis, and knowledge synthesis",
                recognition_keywords=[
                    "research", "analyze", "investigate", "study", "explore",
                    "data", "evidence", "findings", "conclusion", "hypothesis",
                    "literature", "review", "academic", "scientific",
                ],
                primary_goal="Generate comprehensive understanding and evidence-based insights",
                evaluation_criteria=[
                    "Depth of analysis", "Quality of sources",
                    "Logical reasoning", "Novel insights generated",
                ],
                preferred_tools=[
                    "grokipedia_search", "brain_network_search", "analyze_topic",
                    "search_knowledge", "extract_content",
                ],
                chain_phases=[
                    "literature_review", "data_analysis", "synthesis",
                    "conclusion", "recommendations",
                ],
                prompt_templates={
                    "exploration": "Conduct thorough research on: {topic}. Identify key findings, methodologies, and implications.",
                    "selection": "Analyze the most promising research directions and evidence-based approaches.",
                    "specification": "Develop detailed research methodology and data collection strategies.",
                    "output": "Synthesize findings into comprehensive research paper format with citations and implications.",
                },
                success_metrics=[
                    "Number of sources analyzed", "Novel insights generated",
                    "Strength of conclusions", "Practical implications identified",
                ],
                priority_tools={"grokipedia_search": 10, "brain_network_search": 9, "analyze_topic": 8},
            ),
            TaskType.CREATIVE_WRITING: TaskConfiguration(
                task_type=TaskType.CREATIVE_WRITING,
                name="Creative Writing",
                description="Story development, character creation, and narrative crafting",
                recognition_keywords=[
                    "write", "story", "character", "plot", "narrative",
                    "script", "dialogue", "creative", "fiction", "scene",
                    "novel", "screenplay", "drama",
                ],
                primary_goal="Create engaging, original stories and characters",
                evaluation_criteria=[
                    "Creativity and originality", "Character development",
                    "Plot coherence", "Emotional impact",
                ],
                preferred_tools=[
                    "brain_network_search", "grokipedia_search", "analyze_topic",
                    "search_knowledge", "store_learning",
                ],
                chain_phases=[
                    "concept_development", "character_creation", "plot_structure",
                    "scene_writing", "revision_editing",
                ],
                prompt_templates={
                    "exploration": "Brainstorm creative concepts for: {topic}.",
                    "selection": "Select the most compelling creative direction.",
                    "specification": "Develop character profiles, plot outlines, story structure.",
                    "output": "Write complete creative piece with engaging narrative.",
                },
                success_metrics=[
                    "Originality", "Character depth", "Narrative engagement", "Emotional resonance",
                ],
                priority_tools={
                    "create_creative_file": 10, "append_to_creative_file": 9,
                    "write_to_creative_file": 9, "brain_network_search": 7,
                },
            ),
            TaskType.STRATEGIC_PLANNING: TaskConfiguration(
                task_type=TaskType.STRATEGIC_PLANNING,
                name="Strategic Planning",
                description="Business strategy, planning, and decision-making",
                recognition_keywords=[
                    "strategy", "plan", "business", "market", "competitive",
                    "growth", "roadmap", "goals", "objectives", "tactics",
                ],
                primary_goal="Develop comprehensive strategic plans with actionable roadmaps",
                evaluation_criteria=[
                    "Strategic coherence", "Market depth", "Actionable recs", "Risk assessment",
                ],
                preferred_tools=["grokipedia_search", "brain_network_search", "analyze_topic"],
                chain_phases=[
                    "market_analysis", "competitive_assessment", "strategy_formulation",
                    "implementation_planning", "risk_evaluation",
                ],
                prompt_templates={
                    "exploration": "Analyze the strategic landscape for: {topic}.",
                    "selection": "Evaluate strategic options and select most viable.",
                    "specification": "Develop detailed strategic plans with timelines.",
                    "output": "Create comprehensive strategic roadmap.",
                },
                success_metrics=["Insight quality", "Feasibility", "Risk mitigation", "Competitive advantage"],
                priority_tools={"grokipedia_search": 9, "brain_network_search": 10, "analyze_topic": 8},
            ),
            TaskType.PROBLEM_SOLVING: TaskConfiguration(
                task_type=TaskType.PROBLEM_SOLVING,
                name="Problem Solving",
                description="Complex problem analysis and solution development",
                recognition_keywords=[
                    "problem", "solve", "solution", "issue", "challenge",
                    "fix", "resolve", "troubleshoot", "optimize", "improve",
                    "debug", "analysis",
                ],
                primary_goal="Identify root causes and develop effective solutions",
                evaluation_criteria=[
                    "Problem definition", "Root cause depth", "Solution effectiveness", "Implementation practicality",
                ],
                preferred_tools=["analyze_topic", "brain_network_search", "grokipedia_search"],
                chain_phases=[
                    "problem_definition", "root_cause_analysis", "solution_exploration",
                    "solution_evaluation", "implementation_planning",
                ],
                prompt_templates={
                    "exploration": "Define and analyze the core problem in: {topic}.",
                    "selection": "Evaluate root causes and select most likely.",
                    "specification": "Develop solution approaches with pros/cons.",
                    "output": "Present comprehensive analysis with recommended solution.",
                },
                success_metrics=["Understanding accuracy", "Creativity", "Feasibility", "Expected impact"],
                priority_tools={"analyze_topic": 10, "brain_network_search": 9, "grokipedia_search": 8},
            ),
            TaskType.LEARNING_EDUCATION: TaskConfiguration(
                task_type=TaskType.LEARNING_EDUCATION,
                name="Learning & Education",
                description="Educational content creation and learning optimization",
                recognition_keywords=[
                    "learn", "teach", "education", "tutorial", "course",
                    "training", "curriculum", "lesson", "study",
                ],
                primary_goal="Create effective learning experiences",
                evaluation_criteria=["Objective clarity", "Comprehensibility", "Engagement", "Retention"],
                preferred_tools=["brain_network_search", "grokipedia_search", "analyze_topic"],
                chain_phases=[
                    "learning_objectives", "content_structure", "material_development",
                    "assessment_creation", "delivery_optimization",
                ],
                prompt_templates={
                    "exploration": "Identify learning needs for: {topic}.",
                    "selection": "Choose effective teaching approaches.",
                    "specification": "Develop lesson plans and materials.",
                    "output": "Create complete educational package.",
                },
                success_metrics=["Objective achievement", "Clarity", "Assessment effectiveness"],
                priority_tools={"brain_network_search": 10, "grokipedia_search": 8, "analyze_topic": 7},
            ),
            TaskType.TECHNICAL_DEVELOPMENT: TaskConfiguration(
                task_type=TaskType.TECHNICAL_DEVELOPMENT,
                name="Technical Development",
                description="Software development, system design, and technical implementation",
                recognition_keywords=[
                    "code", "develop", "software", "programming", "technical",
                    "implementation", "architecture", "system", "api", "database",
                    "algorithm", "framework",
                ],
                primary_goal="Design and implement robust technical solutions",
                evaluation_criteria=["Feasibility", "Code quality", "Performance", "Scalability"],
                preferred_tools=["grokipedia_search", "brain_network_search", "analyze_topic"],
                chain_phases=[
                    "requirements_analysis", "architecture_design", "implementation_planning",
                    "development_execution", "testing_validation",
                ],
                prompt_templates={
                    "exploration": "Analyze technical requirements for: {topic}.",
                    "selection": "Evaluate approaches, select optimal architecture.",
                    "specification": "Create technical specifications and APIs.",
                    "output": "Deliver complete solution with code and docs.",
                },
                success_metrics=["Solution quality", "Completeness", "Performance", "Maintainability"],
                priority_tools={"grokipedia_search": 9, "brain_network_search": 8, "analyze_topic": 10},
            ),
            TaskType.TECHNICAL_INNOVATION: TaskConfiguration(
                task_type=TaskType.TECHNICAL_INNOVATION,
                name="Technical Innovation",
                description="Creating novel technical solutions, prototypes, and innovations",
                recognition_keywords=[
                    "innovate", "create", "build", "prototype", "innovation",
                    "novel", "advance", "breakthrough", "invention", "product",
                ],
                primary_goal="Create tangible technical innovations",
                evaluation_criteria=["Feasibility", "Novelty", "Practicality", "Effectiveness"],
                preferred_tools=["grokipedia_search", "brain_network_search", "analyze_topic"],
                chain_phases=[
                    "concept_exploration", "feasibility_analysis", "design_creation",
                    "prototype_development", "testing_validation",
                ],
                prompt_templates={
                    "exploration": "Explore innovative approaches for: {topic}.",
                    "selection": "Evaluate most promising solutions.",
                    "specification": "Design and create detailed specs and prototypes.",
                    "output": "Deliver complete working solutions.",
                },
                success_metrics=["Originality", "Completeness", "Implementation success", "Functionality"],
                priority_tools={"grokipedia_search": 9, "brain_network_search": 8, "analyze_topic": 7},
            ),
            TaskType.BUSINESS_DEVELOPMENT: TaskConfiguration(
                task_type=TaskType.BUSINESS_DEVELOPMENT,
                name="Business Development",
                description="Revenue generation, market expansion, and business growth",
                recognition_keywords=[
                    "business", "revenue", "sales", "market", "growth",
                    "monetize", "profit", "customer", "pricing", "marketing",
                    "expansion", "money", "income", "financial",
                ],
                primary_goal="Develop profitable business models and growth strategies",
                evaluation_criteria=["Revenue potential", "Market opportunity", "Positioning", "Sustainability"],
                preferred_tools=["grokipedia_search", "brain_network_search", "analyze_topic"],
                chain_phases=[
                    "market_opportunity", "business_model_design", "revenue_strategy",
                    "growth_planning", "risk_assessment",
                ],
                prompt_templates={
                    "exploration": "Analyze market opportunities for: {topic}.",
                    "selection": "Evaluate business models, select most viable.",
                    "specification": "Develop business plans with pricing and positioning.",
                    "output": "Create comprehensive business development plan.",
                },
                success_metrics=["Revenue viability", "Market penetration", "Acquisition efficiency", "Sustainability"],
                priority_tools={"grokipedia_search": 10, "brain_network_search": 9, "analyze_topic": 8},
            ),
        }
