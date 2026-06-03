"""
SAIGE Task Router — Intelligent Task-to-Agent Matching
========================================================
Routes incoming user tasks to the best-fit marketplace agent based on:
  - Department keyword matching
  - Role title relevance
  - Focus area similarity
  - Agent workload (tasks already assigned)
  - Agent status (active, not paused/retired)

This is the bridge between TaskSystem (task_queue.json) and the
persistent agent daemon (daemon_state.json). Without this, user tasks
go into a queue that nobody ever reads.

Usage:
    from task_router import TaskRouter
    router = TaskRouter(daemon)
    agent_id = router.route_and_assign(task)
"""

import re
import time
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("SAIGE.TaskRouter")

# ── Department keyword mappings ──────────────────────────────────
# Maps common user-facing keywords to department IDs so we can match
# "write me a blog post" → content/creative, "audit my contract" → legal, etc.

DEPARTMENT_KEYWORDS: Dict[str, List[str]] = {
    "finance": [
        "finance", "financial", "investment", "portfolio", "stock", "trading",
        "tax", "taxes", "accounting", "budget", "revenue", "profit", "loss",
        "cryptocurrency", "crypto", "defi", "audit", "expense", "payroll",
        "invoice", "banking", "loan", "mortgage", "insurance", "valuation",
    ],
    "development": [
        "code", "coding", "programming", "developer", "software", "app",
        "application", "api", "backend", "frontend", "fullstack", "devops",
        "docker", "kubernetes", "deploy", "deployment", "debug", "bug",
        "repository", "git", "python", "javascript", "typescript", "react",
        "node", "database", "sql", "server", "microservice", "ci/cd",
        "architecture", "refactor", "testing", "unit test",
        "scrape", "scraper", "web scraping", "script", "automation script",
        "flask", "django", "fastapi", "rust", "golang", "java", "c++",
    ],
    "content": [
        "content", "blog", "article", "copywriting", "content writing",
        "seo", "social media", "newsletter", "editorial", "press release",
        "story", "narrative", "screenplay", "copy", "blog post",
        "headline", "caption", "ghostwrite", "proofread",
    ],
    "research": [
        "research", "study", "analysis", "analyze", "investigate", "survey",
        "report", "whitepaper", "white paper", "literature review", "data",
        "statistics", "findings", "methodology", "hypothesis", "experiment",
        "benchmark", "compare", "comparison", "market research", "trend",
    ],
    "support": [
        "support", "customer", "help desk", "ticket", "troubleshoot",
        "onboarding", "faq", "knowledge base", "chat support", "escalation",
        "complaint", "feedback", "satisfaction", "response", "sla",
    ],
    "legal": [
        "legal", "law", "lawyer", "attorney", "contract", "compliance",
        "regulation", "gdpr", "privacy", "terms of service", "tos",
        "intellectual property", "ip", "patent", "trademark", "copyright",
        "litigation", "dispute", "nda", "agreement", "policy",
        "clause", "liability", "indemnification", "arbitration", "template",
    ],
    "healthcare": [
        "health", "healthcare", "medical", "clinical", "patient", "diagnosis",
        "treatment", "therapy", "pharmaceutical", "drug", "biotech",
        "telemedicine", "ehr", "hipaa", "wellness", "mental health",
        "nutrition", "fitness", "epidemiology", "public health",
    ],
    "education": [
        "education", "learning", "course", "curriculum", "training",
        "tutorial", "lesson", "teach", "student", "school", "university",
        "e-learning", "elearning", "lms", "assessment", "quiz", "exam",
        "certification", "workshop", "seminar", "lecture", "pedagogy",
    ],
    "ecommerce": [
        "ecommerce", "e-commerce", "store", "shop", "product", "catalog",
        "inventory", "order", "checkout", "cart", "payment", "shipping",
        "fulfillment", "marketplace", "listing", "price", "pricing",
        "dropshipping", "retail", "wholesale", "subscription",
    ],
    "data": [
        "data", "analytics", "dashboard", "visualization", "bi",
        "business intelligence", "etl", "pipeline", "warehouse",
        "machine learning", "ml", "ai", "model", "prediction",
        "classification", "regression", "clustering", "nlp",
        "neural network", "deep learning", "tensorflow", "pytorch",
    ],
    "creative": [
        "design", "creative", "graphic", "ui", "ux", "user interface",
        "user experience", "logo", "brand", "branding", "illustration",
        "animation", "video", "photo", "image", "visual", "mockup",
        "wireframe", "prototype", "figma", "photoshop", "color",
        "typography", "layout", "poster", "banner", "infographic",
    ],
    "real_estate": [
        "real estate", "property", "listing", "rental", "lease",
        "mortgage", "appraisal", "zoning", "commercial property",
        "residential", "tenant", "landlord", "roi", "cap rate",
    ],
    "hr": [
        "hr", "human resources", "hiring", "recruit", "recruitment",
        "talent", "interview", "resume", "cv", "job description",
        "onboarding", "offboarding", "performance review", "compensation",
        "benefits", "payroll", "culture", "diversity", "inclusion",
        "employee", "workforce", "retention",
    ],
    "marketing": [
        "marketing", "campaign", "advertising", "ad", "ads", "ppc",
        "cpc", "conversion", "funnel", "lead", "leads", "email marketing",
        "growth", "acquisition", "retention", "engagement", "influencer",
        "affiliate", "brand awareness", "market", "target audience",
        "persona", "a/b test", "ab test", "crm",
    ],
    "operations": [
        "operations", "ops", "supply chain", "logistics", "procurement",
        "vendor", "quality", "process", "workflow", "automation",
        "efficiency", "lean", "six sigma", "kpi", "metrics",
        "project management", "agile", "scrum", "kanban", "timeline",
    ],
    "personal": [
        "personal", "assistant", "schedule", "calendar", "reminder",
        "travel", "booking", "reservation", "errand", "organize",
        "plan", "planning", "productivity", "time management", "todo",
        "to-do", "task list", "email", "correspondence",
    ],
    "robotics": [
        "robot", "robotics", "automation", "iot", "sensor", "actuator",
        "ros", "ros2", "hardware", "embedded", "firmware", "plc",
        "control system", "autonomous", "drone", "3d printing",
    ],
    "security": [
        "security", "cybersecurity", "infosec", "penetration test",
        "pentest", "vulnerability", "threat", "firewall", "encryption",
        "authentication", "authorization", "oauth", "sso", "audit",
        "compliance", "incident response", "soc", "siem",
    ],
    "blockchain": [
        "blockchain", "smart contract", "solidity", "web3", "nft",
        "token", "tokenomics", "dao", "defi", "dex", "wallet",
        "ethereum", "solana", "bitcoin", "consensus", "mining",
        "staking", "yield", "liquidity", "bridge",
    ],
    "science": [
        "science", "physics", "chemistry", "biology", "astronomy",
        "mathematics", "math", "equation", "simulation", "laboratory",
        "experiment", "theory", "quantum", "particle", "genome",
        "protein", "climate", "environmental", "geology", "ecology",
    ],
}

# ── Role-specific keyword boosts ──────────────────────────────────
# Some keywords map very precisely to a role WITHIN a department.
# These give extra score to the specific agent, not just the department.

ROLE_KEYWORD_BOOSTS: Dict[str, List[str]] = {
    # Finance
    "portfolio": ["portfolio", "asset allocation", "diversification", "holdings"],
    "tax": ["tax", "taxes", "irs", "deduction", "filing", "w-2", "1099"],
    "crypto": ["cryptocurrency", "crypto", "bitcoin", "ethereum", "defi", "token"],
    # Development
    "frontend": ["react", "vue", "angular", "css", "html", "ui", "frontend"],
    "backend": ["api", "backend", "server", "database", "sql", "rest", "graphql"],
    "devops": ["docker", "kubernetes", "ci/cd", "pipeline", "deploy", "aws", "cloud"],
    # Content
    "seo": ["seo", "search engine", "keywords", "ranking", "backlink", "serp"],
    "copywriting": ["copy", "headline", "conversion", "cta", "landing page"],
    # Marketing
    "email": ["email marketing", "newsletter", "drip", "campaign", "mailchimp"],
    "growth": ["growth", "acquisition", "viral", "referral", "retention"],
    # Data
    "ml": ["machine learning", "model", "training", "prediction", "neural", "deep learning"],
    "analytics": ["dashboard", "visualization", "bi", "tableau", "metrics", "kpi"],
}


class TaskRouter:
    """
    Routes incoming tasks to the best-fit marketplace agent.
    
    The router:
    1. Analyzes task text for department/role keywords
    2. Scores all active agents against the task
    3. Picks the highest-scoring agent with lowest workload
    4. Assigns the task via TaskSystem
    """

    def __init__(self, daemon):
        """
        Args:
            daemon: AgentDaemon instance (for agent registry access)
        """
        self.daemon = daemon
        self._task_system = None

    def _get_task_system(self):
        """Lazy-load shared TaskSystem instance."""
        if self._task_system is None:
            from repryntt.agents.task_system import TaskSystem
            self._task_system = TaskSystem()
        return self._task_system

    def route_and_assign(self, task) -> Optional[str]:
        """
        Route a task to the best agent and assign it.
        
        Args:
            task: Task object from TaskSystem
            
        Returns:
            agent_id of assigned agent, or None if no match found
        """
        # Score all active agents
        scores = self._score_agents(task)
        if not scores:
            logger.warning(f"⚠️ No active agents to handle task: {task.title}")
            return None

        # Pick the best agent (highest score, tiebreak by lowest workload)
        best_agent_id, best_score, _ = scores[0]
        if best_score <= 0:
            # No keyword match at all — assign to the best general-purpose agent
            # (personal assistant department or first available)
            logger.info(f"No strong match for '{task.title}', using best available agent")

        # Assign
        ts = self._get_task_system()
        ts.assign_task_to_agent(task.id, best_agent_id)
        agent = self.daemon.agents.get(best_agent_id)
        agent_name = agent.display_name or agent.name if agent else best_agent_id
        logger.info(f"🎯 Task '{task.title}' routed to {agent_name} "
                    f"(score={best_score:.1f}, agent={best_agent_id})")
        return best_agent_id

    def route_to_specific_agent(self, task, agent_id: str) -> bool:
        """
        Route a task to a specific agent by ID.
        
        Returns True if assignment succeeded.
        """
        if agent_id not in self.daemon.agents:
            logger.warning(f"⚠️ Agent {agent_id} not found")
            return False

        agent = self.daemon.agents[agent_id]
        if agent.status != "active":
            logger.warning(f"⚠️ Agent {agent_id} is {agent.status}, cannot assign")
            return False

        ts = self._get_task_system()
        ts.assign_task_to_agent(task.id, agent_id)
        logger.info(f"🎯 Task '{task.title}' directly assigned to "
                    f"{agent.display_name or agent.name}")
        return True

    def _score_agents(self, task) -> List[Tuple[str, float, int]]:
        """
        Score all active agents against a task.
        
        Returns list of (agent_id, score, workload) sorted by
        score DESC, workload ASC.
        """
        task_text = f"{task.title} {task.description} {task.deliverable}".lower()
        task_type = (task.task_type or "").lower()
        results = []

        ts = self._get_task_system()

        # Helper: word-boundary match to avoid substring false positives
        # e.g. "ip" should NOT match inside "script"
        def _kw_match(keyword: str, text: str) -> bool:
            return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))

        for agent_id, agent in self.daemon.agents.items():
            if agent.status != "active":
                continue

            score = 0.0

            # 1. Department keyword matching (word-boundary)
            dept = agent.department or agent.role or ""
            dept_keywords = DEPARTMENT_KEYWORDS.get(dept, [])
            dept_match_count = sum(1 for kw in dept_keywords if _kw_match(kw, task_text))
            score += dept_match_count * 3.0  # 3 points per keyword match

            # 2. Role title matching
            role_title = (agent.role_title or "").lower()
            role_words = re.findall(r'\w+', role_title)
            for word in role_words:
                if len(word) > 3 and word in task_text:
                    score += 5.0  # Role title word match is strong signal

            # 3. Focus area matching
            focus = (agent.focus_area or "").lower()
            focus_words = re.findall(r'\w+', focus)
            for word in focus_words:
                if len(word) > 3 and word in task_text:
                    score += 4.0  # Focus area match

            # 4. Role-specific keyword boosts
            for role_key, boost_keywords in ROLE_KEYWORD_BOOSTS.items():
                if role_key in role_title or role_key in focus:
                    boost_count = sum(1 for kw in boost_keywords if _kw_match(kw, task_text))
                    score += boost_count * 2.0

            # 5. Task type matching
            if task_type:
                type_dept_map = {
                    "research": ["research", "data", "science"],
                    "code": ["development", "data", "robotics"],
                    "creative": ["creative", "content", "marketing"],
                    "analysis": ["data", "research", "finance"],
                    "system": ["development", "operations", "security"],
                    "learning": ["education", "research", "science"],
                }
                matching_depts = type_dept_map.get(task_type, [])
                if dept in matching_depts:
                    score += 2.0

            # 6. Workload penalty — agents with many tasks get deprioritized
            agent_tasks = ts.get_tasks_for_agent(agent_id)
            workload = len(agent_tasks)
            score -= workload * 5.0  # Heavy penalty per existing task

            results.append((agent_id, score, workload))

        # Sort: highest score first, then lowest workload
        results.sort(key=lambda x: (-x[1], x[2]))
        return results

    def find_best_agents(self, task_text: str, top_n: int = 5) -> List[Dict]:
        """
        Find the best agents for a task description (for UI suggestions).
        
        Returns list of dicts with agent info + match score.
        """
        from repryntt.agents.task_system import Task
        dummy_task = Task(title=task_text, description=task_text)
        scores = self._score_agents(dummy_task)

        results = []
        for agent_id, score, workload in scores[:top_n]:
            agent = self.daemon.agents.get(agent_id)
            if agent:
                results.append({
                    "agent_id": agent_id,
                    "name": agent.display_name or agent.name,
                    "role_title": agent.role_title,
                    "department": agent.department,
                    "focus_area": agent.focus_area,
                    "score": round(score, 1),
                    "current_workload": workload,
                })
        return results

    def get_agent_workload_summary(self) -> Dict[str, int]:
        """Get task count per agent for load balancing visibility."""
        ts = self._get_task_system()
        summary = {}
        for agent_id in self.daemon.agents:
            tasks = ts.get_tasks_for_agent(agent_id)
            if tasks:
                summary[agent_id] = len(tasks)
        return summary
