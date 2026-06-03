"""
agent_departments.py
═════════════════════════════════════════════════════════════════════
Agent Department & Identity System for REPRYNTT

Defines professional departments, roles, and identity generation
for the 164-agent marketplace. Each agent gets a department, role
title, focus area, and alphanumeric code name (e.g. "SD-001").

Extracted from the former kardashev_civilization.py — simulation
and progression systems removed.
═════════════════════════════════════════════════════════════════════
"""

import random
import logging
from typing import Dict, Tuple

logger = logging.getLogger("saige.departments")


# ════════════════════════════════════════════════════════════════════
# SCIENTIFIC DEPARTMENTS (12)
# ════════════════════════════════════════════════════════════════════

DEPARTMENTS = {
    "energy_physics": {
        "name": "Energy & Plasma Physics",
        "short": "Energy",
        "description": "Fusion research, plasma containment, reactor design, energy storage, grid systems.",
        "roles": [
            {"title": "Plasma Physicist", "focus": "magnetic confinement, tokamak optimization, plasma instabilities"},
            {"title": "Fusion Engineer", "focus": "reactor design, materials under neutron bombardment, tritium breeding"},
            {"title": "Energy Storage Researcher", "focus": "supercapacitors, grid-scale batteries, flywheel systems"},
            {"title": "Grid Systems Architect", "focus": "power distribution, smart grids, load balancing, fault tolerance"},
            {"title": "Nuclear Engineer", "focus": "fission-fusion hybrids, reactor safety, waste transmutation"},
            {"title": "Laser Physicist", "focus": "inertial confinement fusion, high-energy laser systems"},
        ],
    },
    "materials_science": {
        "name": "Materials Science & Engineering",
        "short": "Materials",
        "description": "Advanced materials, superconductors, metamaterials, nanofabrication.",
        "roles": [
            {"title": "Materials Scientist", "focus": "high-temperature superconductors, radiation-resistant alloys"},
            {"title": "Nanoengineer", "focus": "molecular assembly, nanostructured materials, self-healing surfaces"},
            {"title": "Metallurgist", "focus": "tungsten composites, first-wall materials, heat-resistant alloys"},
            {"title": "Polymer Chemist", "focus": "advanced polymers, thermal shielding, flexible electronics"},
            {"title": "Ceramics Engineer", "focus": "plasma-facing ceramics, thermal barrier coatings"},
        ],
    },
    "computational_science": {
        "name": "Computational Science & AI",
        "short": "Computing",
        "description": "Simulation, machine learning, quantum computing, AI systems, software engineering.",
        "roles": [
            {"title": "Computational Physicist", "focus": "plasma simulation, finite element analysis, Monte Carlo methods"},
            {"title": "AI Research Scientist", "focus": "reinforcement learning, multi-agent systems, neural architecture"},
            {"title": "Quantum Computing Researcher", "focus": "quantum algorithms, error correction, quantum simulation"},
            {"title": "Software Architect", "focus": "simulation frameworks, distributed computing, scientific software"},
            {"title": "Data Scientist", "focus": "experimental data analysis, pattern recognition, predictive modeling"},
            {"title": "ML Engineer", "focus": "training pipelines, model optimization, inference systems"},
            {"title": "Systems Programmer", "focus": "CUDA kernels, HPC clusters, OS-level optimization"},
        ],
    },
    "aerospace": {
        "name": "Aerospace & Space Systems",
        "short": "Aerospace",
        "description": "Rocket propulsion, orbital mechanics, satellite systems, space habitats.",
        "roles": [
            {"title": "Propulsion Engineer", "focus": "ion drives, nuclear thermal propulsion, solar sails"},
            {"title": "Orbital Mechanics Specialist", "focus": "trajectory optimization, orbital transfer, Lagrange points"},
            {"title": "Spacecraft Systems Engineer", "focus": "thermal control, life support, power systems integration"},
            {"title": "Mission Planner", "focus": "interplanetary mission design, delta-v budgets, launch windows"},
            {"title": "Satellite Engineer", "focus": "communication satellites, Earth observation, solar power satellites"},
            {"title": "Space Habitat Designer", "focus": "rotating habitats, radiation shielding, closed-loop ecosystems"},
        ],
    },
    "climate_planetary": {
        "name": "Climate & Planetary Science",
        "short": "Climate",
        "description": "Atmospheric science, climate modeling, geoengineering, ecology.",
        "roles": [
            {"title": "Climate Modeler", "focus": "GCM simulation, feedback loops, tipping points"},
            {"title": "Atmospheric Scientist", "focus": "atmospheric dynamics, carbon capture, ozone chemistry"},
            {"title": "Geoengineering Researcher", "focus": "solar radiation management, ocean fertilization, albedo modification"},
            {"title": "Ecologist", "focus": "ecosystem modeling, biodiversity metrics, biosphere recovery"},
            {"title": "Oceanographer", "focus": "thermohaline circulation, ocean heat content, marine ecosystems"},
        ],
    },
    "biotech_medical": {
        "name": "Biotechnology & Medical Sciences",
        "short": "Biotech",
        "description": "Genetic engineering, synthetic biology, medical research, longevity science.",
        "roles": [
            {"title": "Geneticist", "focus": "gene editing, CRISPR systems, synthetic genomes"},
            {"title": "Bioprocess Engineer", "focus": "bioreactor design, fermentation, scale-up processes"},
            {"title": "Medical Researcher", "focus": "drug discovery, clinical trial design, disease modeling"},
            {"title": "Synthetic Biologist", "focus": "metabolic engineering, biological computing, artificial cells"},
            {"title": "Neuroscientist", "focus": "neural interfaces, brain mapping, cognitive enhancement"},
        ],
    },
    "economics_governance": {
        "name": "Economics & Governance",
        "short": "Economics",
        "description": "Resource allocation, economic modeling, policy design, governance frameworks.",
        "roles": [
            {"title": "Economist", "focus": "resource economics, growth modeling, market design"},
            {"title": "Policy Analyst", "focus": "technology policy, regulation design, impact assessment"},
            {"title": "Urban Planner", "focus": "city design, infrastructure planning, population dynamics"},
            {"title": "Supply Chain Analyst", "focus": "logistics optimization, resource flow, bottleneck analysis"},
            {"title": "Governance Researcher", "focus": "voting systems, decision theory, institutional design"},
        ],
    },
    "electrical_engineering": {
        "name": "Electrical & Power Engineering",
        "short": "Electrical",
        "description": "Power electronics, superconducting magnets, RF systems, instrumentation.",
        "roles": [
            {"title": "Power Electronics Engineer", "focus": "inverters, converters, high-voltage systems"},
            {"title": "Superconducting Magnet Engineer", "focus": "HTS magnets, quench protection, field design"},
            {"title": "RF Engineer", "focus": "plasma heating, waveguides, antenna design"},
            {"title": "Control Systems Engineer", "focus": "PID tuning, real-time control, feedback systems"},
            {"title": "Instrumentation Scientist", "focus": "diagnostics, sensors, measurement systems"},
        ],
    },
    "mathematics_theory": {
        "name": "Mathematics & Theoretical Physics",
        "short": "Mathematics",
        "description": "Pure mathematics, theoretical physics, optimization theory, foundations.",
        "roles": [
            {"title": "Theoretical Physicist", "focus": "quantum field theory, condensed matter, statistical mechanics"},
            {"title": "Applied Mathematician", "focus": "PDE solvers, optimization, numerical methods"},
            {"title": "Topologist", "focus": "knot theory, manifold classification, applied topology"},
            {"title": "Statistician", "focus": "Bayesian inference, experimental design, uncertainty quantification"},
            {"title": "Complexity Theorist", "focus": "computational complexity, algorithm design, information theory"},
        ],
    },
    "robotics_automation": {
        "name": "Robotics & Automation",
        "short": "Robotics",
        "description": "Autonomous systems, industrial automation, drones, manufacturing robots.",
        "roles": [
            {"title": "Robotics Engineer", "focus": "manipulator design, locomotion, swarm robotics"},
            {"title": "Automation Specialist", "focus": "PLC programming, SCADA systems, process automation"},
            {"title": "Mechatronics Engineer", "focus": "actuators, sensors, embedded systems integration"},
            {"title": "Drone Systems Engineer", "focus": "UAV design, autonomous navigation, fleet coordination"},
            {"title": "Manufacturing Engineer", "focus": "CNC, additive manufacturing, quality control"},
        ],
    },
    "communications": {
        "name": "Communications & Information",
        "short": "Comms",
        "description": "Networking, signal processing, cryptography, interplanetary communication.",
        "roles": [
            {"title": "Network Architect", "focus": "mesh networks, latency optimization, protocol design"},
            {"title": "Signal Processing Engineer", "focus": "DSP, antenna arrays, noise reduction"},
            {"title": "Cryptographer", "focus": "post-quantum cryptography, secure communications, key exchange"},
            {"title": "Information Theorist", "focus": "channel capacity, data compression, error-correcting codes"},
        ],
    },
    "mining_resources": {
        "name": "Mining & Resource Extraction",
        "short": "Mining",
        "description": "Asteroid mining, in-situ resource utilization, geological survey, refining.",
        "roles": [
            {"title": "Mining Engineer", "focus": "extraction techniques, tunnel design, resource estimation"},
            {"title": "Geologist", "focus": "mineral survey, geological mapping, resource classification"},
            {"title": "Refining Chemist", "focus": "ore processing, electrochemistry, separation processes"},
            {"title": "ISRU Specialist", "focus": "in-situ resource utilization, lunar/asteroid regolith processing"},
        ],
    },
}


# ════════════════════════════════════════════════════════════════════
# MARKETPLACE DEPARTMENTS — 158 Agent Job Categories in 20 Groups
# ════════════════════════════════════════════════════════════════════

MARKETPLACE_DEPARTMENTS = {
    "finance_trading": {
        "name": "Finance & Trading",
        "short": "Finance",
        "description": "Crypto trading, DeFi, portfolio management, financial analysis, tax and invoicing.",
        "roles": [
            {"title": "Memecoin/Crypto Trader", "focus": "scan launches, analyze on-chain data, execute trades, manage positions"},
            {"title": "DeFi Yield Farmer", "focus": "find optimal yield strategies, auto-compound, rebalance across protocols"},
            {"title": "Arbitrage Detector", "focus": "cross-DEX price differences, CEX-DEX arbitrage, triangular arb"},
            {"title": "Portfolio Manager", "focus": "rebalancing, risk assessment, allocation optimization"},
            {"title": "Token Due Diligence Analyst", "focus": "smart contract audit summaries, rug-pull detection, holder analysis"},
            {"title": "NFT Analyst", "focus": "floor price monitoring, rarity analysis, auto-bidding"},
            {"title": "Crypto Tax Specialist", "focus": "track cost basis across wallets, generate tax reports"},
            {"title": "Invoice Processor", "focus": "read invoices, categorize expenses, reconcile against statements"},
            {"title": "Financial Modeler", "focus": "build projections, sensitivity analysis, unit economics models"},
            {"title": "Personal Budget Advisor", "focus": "track spending, categorize, alert on anomalies, suggest savings"},
        ],
    },
    "software_development": {
        "name": "Software Development",
        "short": "SoftDev",
        "description": "Code generation, debugging, testing, documentation, DevOps, and API development.",
        "roles": [
            {"title": "Code Generator", "focus": "full feature implementation from requirements"},
            {"title": "Bug Fixer", "focus": "analyze error logs, trace root cause, write patches"},
            {"title": "Code Reviewer", "focus": "review PRs, flag issues, suggest improvements"},
            {"title": "Test Engineer", "focus": "generate unit tests, integration tests, edge case coverage"},
            {"title": "Documentation Writer", "focus": "API docs, README files, inline comments from code"},
            {"title": "Migration Specialist", "focus": "Python 2-to-3, JS-to-TS, framework upgrades"},
            {"title": "Database Architect", "focus": "schema design, migration scripts, query optimization"},
            {"title": "DevOps Engineer", "focus": "CI/CD pipeline creation, Docker configs, deployment scripts"},
            {"title": "Security Auditor", "focus": "scan code for vulnerabilities, generate fix recommendations"},
            {"title": "API Developer", "focus": "design REST/GraphQL APIs, generate OpenAPI specs, build endpoints"},
            {"title": "Legacy Code Refactorer", "focus": "modernize old codebases, improve architecture"},
            {"title": "Mobile App Developer", "focus": "React Native, Flutter code generation"},
            {"title": "Smart Contract Developer", "focus": "Solidity, Rust/Anchor development, auditing, deployment"},
        ],
    },
    "content_creation": {
        "name": "Content Creation",
        "short": "Content",
        "description": "Writing, social media, copywriting, SEO, translation, and creative content.",
        "roles": [
            {"title": "Blog/Article Writer", "focus": "SEO-optimized long-form content on any topic"},
            {"title": "Social Media Manager", "focus": "write posts, schedule content, respond to comments"},
            {"title": "Email Copywriter", "focus": "sales sequences, newsletters, cold outreach"},
            {"title": "Ad Copy Specialist", "focus": "Facebook, Google, TikTok ad variants, A/B test generation"},
            {"title": "Product Description Writer", "focus": "e-commerce listings, Amazon/Shopify optimization"},
            {"title": "Script Writer", "focus": "YouTube scripts, podcast outlines, video narration"},
            {"title": "Ghostwriter", "focus": "books, whitepapers, thought leadership pieces"},
            {"title": "Press Release Writer", "focus": "company announcements, product launches, event coverage"},
            {"title": "Resume/Cover Letter Writer", "focus": "tailored to specific job listings"},
            {"title": "Grant Writer", "focus": "research proposals, nonprofit grant applications"},
            {"title": "Translator", "focus": "multi-language content adaptation, not just word-for-word"},
            {"title": "SEO Optimizer", "focus": "keyword research, content gap analysis, meta tag generation"},
        ],
    },
    "research_analysis": {
        "name": "Research & Analysis",
        "short": "Research",
        "description": "Academic research, market analysis, patent research, data analysis, and trend forecasting.",
        "roles": [
            {"title": "Academic Literature Reviewer", "focus": "search papers, summarize findings, identify gaps"},
            {"title": "Market Researcher", "focus": "competitor analysis, market sizing, trend identification"},
            {"title": "Patent Researcher", "focus": "prior art search, patent landscape mapping"},
            {"title": "Competitive Intelligence Analyst", "focus": "monitor competitor websites, pricing, product changes"},
            {"title": "Data Analyst", "focus": "statistical analysis, visualization, insight extraction from datasets"},
            {"title": "Survey Designer", "focus": "create questionnaires, analyze responses, generate reports"},
            {"title": "Fact Checker", "focus": "verify claims against sources, rate confidence levels"},
            {"title": "News Monitor", "focus": "track industry news, generate daily briefings"},
            {"title": "Sentiment Analyst", "focus": "brand monitoring across social media, review analysis"},
            {"title": "Trend Forecaster", "focus": "analyze historical data, project future trends"},
        ],
    },
    "customer_service": {
        "name": "Customer Service & Support",
        "short": "Support",
        "description": "Tier 1 support, ticket triage, knowledge base, chatbot, onboarding, scheduling.",
        "roles": [
            {"title": "Tier 1 Support Agent", "focus": "answer FAQs, troubleshoot common issues, escalate when needed"},
            {"title": "Ticket Triage Specialist", "focus": "classify incoming support tickets, route to correct team, prioritize"},
            {"title": "Knowledge Base Creator", "focus": "generate help articles from support ticket patterns"},
            {"title": "Chatbot Operator", "focus": "24/7 customer-facing chat with context awareness"},
            {"title": "Review Response Agent", "focus": "respond to Google/Yelp/Amazon reviews professionally"},
            {"title": "Onboarding Assistant", "focus": "guide new users through product setup"},
            {"title": "Returns/Refunds Processor", "focus": "handle return requests per policy rules"},
            {"title": "Appointment Scheduler", "focus": "manage calendars, confirm bookings, send reminders"},
        ],
    },
    "legal": {
        "name": "Legal",
        "short": "Legal",
        "description": "Contract review, legal research, compliance monitoring, and document generation.",
        "roles": [
            {"title": "Contract Reviewer", "focus": "flag risky clauses, summarize terms, compare against standards"},
            {"title": "Legal Researcher", "focus": "case law search, statute interpretation, precedent analysis"},
            {"title": "Contract Drafter", "focus": "generate NDAs, standard agreements from parameters"},
            {"title": "Compliance Monitor", "focus": "track regulatory changes, flag impact on business"},
            {"title": "Terms of Service Generator", "focus": "privacy policies, ToS, cookie policies"},
            {"title": "Trademark Researcher", "focus": "check availability, identify conflicts"},
            {"title": "Discovery Document Reviewer", "focus": "process large document sets, flag relevant materials"},
            {"title": "Immigration Form Assistant", "focus": "help fill out visa applications, check requirements"},
        ],
    },
    "healthcare": {
        "name": "Healthcare",
        "short": "Health",
        "description": "Medical literature, patient intake, insurance claims, clinical trial matching.",
        "roles": [
            {"title": "Medical Literature Summarizer", "focus": "summarize research papers for clinicians"},
            {"title": "Patient Intake Processor", "focus": "organize intake forms, flag relevant history"},
            {"title": "Insurance Claim Processor", "focus": "verify codes, check coverage, process paperwork"},
            {"title": "Healthcare Scheduler", "focus": "patient communication management, appointment reminders"},
            {"title": "Drug Interaction Checker", "focus": "cross-reference medication lists against databases"},
            {"title": "Clinical Trial Matcher", "focus": "match patient profiles to eligible trials"},
            {"title": "Medical Transcriber", "focus": "convert doctor notes to structured records"},
        ],
    },
    "education": {
        "name": "Education",
        "short": "Edu",
        "description": "Tutoring, curriculum design, grading, exam generation, study aids, language learning.",
        "roles": [
            {"title": "Tutor", "focus": "personalized explanations, practice problems, adapts to student level"},
            {"title": "Curriculum Designer", "focus": "create lesson plans, course outlines, learning objectives"},
            {"title": "Grading Assistant", "focus": "grade essays, provide detailed feedback, check rubric alignment"},
            {"title": "Quiz/Exam Generator", "focus": "create assessments with answer keys from source material"},
            {"title": "Study Guide Creator", "focus": "summarize textbook chapters, create flashcards"},
            {"title": "Language Learning Coach", "focus": "conversational practice, grammar correction, vocabulary building"},
            {"title": "Homework Helper", "focus": "step-by-step problem solving with explanation"},
            {"title": "Plagiarism Analyst", "focus": "compare submissions against source material"},
        ],
    },
    "ecommerce_business": {
        "name": "E-Commerce & Business",
        "short": "Commerce",
        "description": "Product sourcing, inventory, pricing, dropshipping, business plans, lead generation.",
        "roles": [
            {"title": "Product Sourcing Agent", "focus": "find suppliers, compare prices, evaluate reliability"},
            {"title": "Inventory Manager", "focus": "predict demand, optimize stock levels, reorder alerts"},
            {"title": "Price Optimizer", "focus": "dynamic pricing based on competition, demand, margins"},
            {"title": "Dropshipping Automator", "focus": "product listing, order forwarding, supplier communication"},
            {"title": "Business Plan Writer", "focus": "financial projections, market analysis, pitch decks"},
            {"title": "Lead Generator", "focus": "scrape directories, qualify prospects, enrich contact data"},
            {"title": "Sales Outreach Agent", "focus": "personalized cold emails at scale"},
            {"title": "CRM Data Entry Agent", "focus": "process emails/calls into structured CRM records"},
            {"title": "Bookkeeper", "focus": "categorize transactions, reconcile accounts, prepare reports"},
            {"title": "Vendor Manager", "focus": "compare quotes, track performance, manage contracts"},
        ],
    },
    "data_processing": {
        "name": "Data Processing",
        "short": "Data",
        "description": "Data entry, cleaning, spreadsheet automation, web scraping, ETL, log analysis.",
        "roles": [
            {"title": "Data Extractor", "focus": "pull structured data from unstructured documents, PDFs, images"},
            {"title": "Data Cleaner", "focus": "normalize formats, deduplicate, fill gaps, validate consistency"},
            {"title": "Spreadsheet Automator", "focus": "complex Excel/Google Sheets formulas, macros, reports"},
            {"title": "Web Scraper", "focus": "extract data from websites, structure it, keep it updated"},
            {"title": "ETL Pipeline Builder", "focus": "transform data between formats and systems"},
            {"title": "OCR Post-Processor", "focus": "clean up OCR output, correct errors, structure data"},
            {"title": "Database Query Specialist", "focus": "natural language to SQL, run queries, format results"},
            {"title": "Log Analyst", "focus": "parse server logs, identify patterns, alert on anomalies"},
        ],
    },
    "creative_design": {
        "name": "Creative & Design",
        "short": "Creative",
        "description": "UI/UX copy, color palettes, wireframes, brand voice, game design, storyboarding.",
        "roles": [
            {"title": "UI/UX Copywriter", "focus": "button text, error messages, onboarding flows, microcopy"},
            {"title": "Color Palette Designer", "focus": "brand-aligned color schemes with accessibility checks"},
            {"title": "Wireframe Describer", "focus": "detailed layout specs from requirements"},
            {"title": "Design System Documenter", "focus": "component specs, usage guidelines, design tokens"},
            {"title": "Brand Voice Developer", "focus": "define tone, style guides, example communications"},
            {"title": "Storyboard Artist", "focus": "scene descriptions for video/animation production"},
            {"title": "Music Lyricist", "focus": "genre-specific, theme-based lyric generation"},
            {"title": "Game Designer", "focus": "mechanics, balancing, narrative design docs"},
        ],
    },
    "real_estate": {
        "name": "Real Estate",
        "short": "RealEst",
        "description": "Property analysis, listing descriptions, lease review, investment analysis.",
        "roles": [
            {"title": "Property Analyst", "focus": "comparable sales, rental yield calculation, market trends"},
            {"title": "Listing Description Writer", "focus": "MLS-optimized property descriptions"},
            {"title": "Lease Reviewer", "focus": "flag tenant-unfriendly clauses, compare to market standard"},
            {"title": "Real Estate Investment Analyst", "focus": "cash flow modeling, cap rate calculation, ROI projections"},
            {"title": "Property Management Communicator", "focus": "tenant communications, maintenance scheduling"},
        ],
    },
    "hr_recruiting": {
        "name": "HR & Recruiting",
        "short": "HR",
        "description": "Resume screening, job descriptions, interview prep, employee handbooks, onboarding.",
        "roles": [
            {"title": "Resume Screener", "focus": "score candidates against job requirements, rank applicants"},
            {"title": "Job Description Writer", "focus": "role-specific, inclusive language, SEO-optimized"},
            {"title": "Interview Question Generator", "focus": "role-specific technical and behavioral questions"},
            {"title": "Employee Handbook Creator", "focus": "policies, procedures, compliance documentation"},
            {"title": "Performance Review Drafter", "focus": "structure feedback from notes, ensure consistency"},
            {"title": "Onboarding Doc Preparer", "focus": "welcome packets, training schedules, checklists"},
        ],
    },
    "marketing": {
        "name": "Marketing",
        "short": "Marketing",
        "description": "Strategy, influencer research, email marketing, landing pages, brand monitoring.",
        "roles": [
            {"title": "Marketing Strategist", "focus": "channel recommendations, budget allocation, campaign planning"},
            {"title": "Influencer Researcher", "focus": "find relevant influencers, analyze engagement, estimate rates"},
            {"title": "Email Marketing Automator", "focus": "segment lists, write sequences, optimize send times"},
            {"title": "Landing Page Copywriter", "focus": "headlines, CTAs, value propositions, A/B variants"},
            {"title": "Brand Monitor", "focus": "track mentions, sentiment, share of voice across platforms"},
            {"title": "Hashtag Researcher", "focus": "platform-specific, trending analysis, competition level"},
            {"title": "Marketing Analytics Reporter", "focus": "pull metrics, generate insights, recommend actions"},
        ],
    },
    "operations_logistics": {
        "name": "Operations & Logistics",
        "short": "Ops",
        "description": "Route optimization, supply chain, process docs, quality control, project management.",
        "roles": [
            {"title": "Route Optimizer", "focus": "delivery routing, fleet scheduling, cost minimization"},
            {"title": "Supply Chain Monitor", "focus": "track shipments, predict delays, suggest alternatives"},
            {"title": "Process Documenter", "focus": "SOPs, workflow diagrams, training materials"},
            {"title": "Quality Control Analyst", "focus": "defect pattern identification, root cause analysis"},
            {"title": "Meeting Summarizer", "focus": "transcribe, extract action items, distribute notes"},
            {"title": "Project Management Assistant", "focus": "task breakdown, timeline estimation, dependency tracking"},
        ],
    },
    "personal_assistant": {
        "name": "Personal Assistant",
        "short": "PA",
        "description": "Email management, travel planning, event planning, home automation, life admin.",
        "roles": [
            {"title": "Email Manager", "focus": "triage inbox, draft responses, summarize threads"},
            {"title": "Travel Planner", "focus": "itinerary creation, flight/hotel comparison, visa requirements"},
            {"title": "Event Planner", "focus": "vendor research, timeline management, guest communication"},
            {"title": "Gift Recommender", "focus": "personalized suggestions based on recipient profile"},
            {"title": "Meal Planner", "focus": "dietary preference-aware, grocery list generation"},
            {"title": "Home Automation Scripter", "focus": "smart home routines, IoT device coordination"},
            {"title": "Life Admin Assistant", "focus": "insurance comparison, utility switching, subscription management"},
        ],
    },
    "robotics_iot": {
        "name": "Robotics & IoT",
        "short": "Robotics",
        "description": "Sensor data analysis, robot task planning, computer vision, predictive maintenance.",
        "roles": [
            {"title": "Sensor Data Analyst", "focus": "process IoT sensor streams, detect anomalies, predict failures"},
            {"title": "Robot Task Planner", "focus": "convert high-level goals to motor command sequences"},
            {"title": "Computer Vision Engineer", "focus": "object detection, classification, tracking workflows"},
            {"title": "Predictive Maintenance Analyst", "focus": "analyze equipment data, schedule maintenance before failure"},
            {"title": "Environmental Monitor", "focus": "air quality, temperature, humidity tracking and alerting"},
            {"title": "Navigation Planner", "focus": "path planning, obstacle avoidance strategy"},
            {"title": "Fleet Coordinator", "focus": "multi-robot task allocation, collision avoidance, load balancing"},
        ],
    },
    "security_compliance": {
        "name": "Security & Compliance",
        "short": "Security",
        "description": "Penetration testing, policy generation, phishing detection, incident response.",
        "roles": [
            {"title": "Pentest Reporter", "focus": "analyze scan results, prioritize findings, write reports"},
            {"title": "Security Policy Generator", "focus": "security policies, access control matrices, incident response plans"},
            {"title": "Phishing Detector", "focus": "analyze emails for social engineering indicators"},
            {"title": "Access Reviewer", "focus": "audit user permissions, flag excessive access, recommend changes"},
            {"title": "Incident Response Agent", "focus": "triage alerts, suggest containment, document timeline"},
            {"title": "Vulnerability Assessor", "focus": "CVE research, impact analysis, patch prioritization"},
        ],
    },
    "blockchain_web3": {
        "name": "Blockchain & Web3",
        "short": "Web3",
        "description": "Smart contract auditing, DAO governance, tokenomics, on-chain analytics, DeFi strategy.",
        "roles": [
            {"title": "Smart Contract Auditor", "focus": "vulnerability scanning, gas optimization, logic verification"},
            {"title": "DAO Governance Analyst", "focus": "proposal drafting, voting analysis, treasury management"},
            {"title": "Tokenomics Designer", "focus": "supply mechanics, distribution models, incentive alignment"},
            {"title": "Airdrop Strategist", "focus": "protocol interaction strategies, eligibility tracking"},
            {"title": "On-Chain Analytics Specialist", "focus": "whale tracking, money flow analysis, protocol TVL monitoring"},
            {"title": "DeFi Strategy Backtester", "focus": "simulate strategies against historical data"},
        ],
    },
    "science_engineering": {
        "name": "Science & Engineering",
        "short": "SciEng",
        "description": "Lab notebooks, simulation optimization, technical specs, CAD, statistical analysis.",
        "roles": [
            {"title": "Lab Notebook Manager", "focus": "structure experimental data, track protocols, analyze results"},
            {"title": "Simulation Optimizer", "focus": "explore parameter spaces, identify optimal configurations"},
            {"title": "Technical Spec Writer", "focus": "engineering specs, requirements docs, standards compliance"},
            {"title": "CAD Spec Generator", "focus": "convert requirements to detailed part specifications"},
            {"title": "Statistical Analyst", "focus": "hypothesis testing, regression analysis, experimental design"},
            {"title": "Environmental Impact Assessor", "focus": "data collection, regulation compliance, report generation"},
        ],
    },
    "video_media_production": {
        "name": "Video & Media Production",
        "short": "Video",
        "description": "End-to-end video content creation: screenwriting, directing, generation, editing, audio, VFX, and QA.",
        "roles": [
            {"title": "Executive Producer", "focus": "orchestrate full production pipeline, manage episode plans, coordinate all departments, enforce quality gates"},
            {"title": "Screenwriter", "focus": "write structured screenplays with scene breakdowns, dialogue, visual directions, and timing cues in JSON-structured format"},
            {"title": "Director", "focus": "break screenplays into shot lists, specify camera angles, transitions, pacing, visual style consistency, and prompt engineering for generation models"},
            {"title": "Video Generator", "focus": "call video generation APIs (grok-imagine-video, Runway, Kling), manage rate limits, collect raw clips, handle retries for failed generations"},
            {"title": "Video Editor", "focus": "assemble clips into timeline via FFmpeg, apply transitions, cuts, color grading, speed ramps, and render final output"},
            {"title": "Audio Engineer", "focus": "generate music/SFX via AI APIs, mix audio tracks, normalize levels, sync audio to video timeline, master final audio"},
            {"title": "Voice Actor", "focus": "generate narration and character dialogue via TTS APIs, control tone/pacing/emotion, lip-sync timing"},
            {"title": "VFX Artist", "focus": "generate visual effects, titles, lower thirds, overlays, motion graphics, and composite layers onto video"},
            {"title": "Thumbnail Designer", "focus": "generate eye-catching thumbnails and promotional stills using image generation, optimize for platform requirements"},
            {"title": "QA Reviewer", "focus": "score visual coherence, continuity, audio sync, pacing quality, flag bad generations for re-roll, approve final cuts"},
        ],
    },
}


# ════════════════════════════════════════════════════════════════════
# DEPARTMENT CODES — 2-letter prefix for agent alphanumeric names
# ════════════════════════════════════════════════════════════════════

DEPT_CODES = {
    "finance_trading": "FT",
    "software_development": "SD",
    "content_creation": "CC",
    "research_analysis": "RA",
    "customer_service": "CS",
    "legal": "LG",
    "healthcare": "HC",
    "education": "ED",
    "ecommerce_business": "EB",
    "data_processing": "DP",
    "creative_design": "CD",
    "real_estate": "RE",
    "hr_recruiting": "HR",
    "marketing": "MK",
    "operations_logistics": "OL",
    "personal_assistant": "PA",
    "robotics_iot": "RI",
    "security_compliance": "SC",
    "blockchain_web3": "BW",
    "science_engineering": "SE",
    "video_media_production": "VP",
    # Legacy scientific departments (fallback)
    "computational_science": "XC",
    "mathematics_theory": "XM",
    "communications": "XO",
    "economics_governance": "XG",
}


# ════════════════════════════════════════════════════════════════════
# NAME GENERATOR
# ════════════════════════════════════════════════════════════════════

def generate_professional_name(department: str, role_title: str,
                                used_names: set = None) -> Tuple[str, str]:
    """
    Generate an alphanumeric default name for an agent.
    Format: "{DEPT_CODE}-{NNN}" e.g. "SD-001", "FT-003"

    Returns (code_name, code_name).
    """
    used = used_names or set()
    dept_code = DEPT_CODES.get(department, "AG")  # fallback "AG" for unknown depts

    # Find next available number for this department prefix
    existing_nums = set()
    for name in used:
        if isinstance(name, str) and name.startswith(f"{dept_code}-"):
            try:
                num = int(name.split("-", 1)[1])
                existing_nums.add(num)
            except (ValueError, IndexError):
                pass

    # Assign next sequential number
    num = 1
    while num in existing_nums:
        num += 1

    code_name = f"{dept_code}-{num:03d}"
    return code_name, code_name


def assign_department_and_role(used_names: set = None,
                                department_counts: Dict[str, int] = None
                                ) -> Dict[str, str]:
    """
    Pick a department and role for a new agent, balancing department sizes.
    Returns dict with department, role_title, focus, full_name, display_name.
    """
    used = used_names or set()
    counts = department_counts or {}

    # Pick department with fewest agents (balancing)
    dept_ids = list(DEPARTMENTS.keys())
    min_count = min(counts.get(d, 0) for d in dept_ids) if counts else 0
    underserved = [d for d in dept_ids if counts.get(d, 0) <= min_count + 2]
    dept_id = random.choice(underserved)

    dept = DEPARTMENTS[dept_id]
    role = random.choice(dept["roles"])

    code_name, display_name = generate_professional_name(dept_id, role["title"], used)

    return {
        "department": dept_id,
        "department_name": dept["name"],
        "role_title": role["title"],
        "focus": role["focus"],
        "full_name": code_name,
        "display_name": display_name,
    }


def regenerate_agent_identities(agents: Dict) -> Dict[str, Dict]:
    """
    Take all existing agents and assign them professional identities.
    Returns a mapping of agent_id -> new identity info.
    """
    used_names: set = set()
    department_counts: Dict[str, int] = {}
    assignments: Dict[str, Dict] = {}

    agent_list = list(agents.values())
    random.shuffle(agent_list)  # Randomize to avoid clustering

    for agent in agent_list:
        identity = assign_department_and_role(used_names, department_counts)
        used_names.add(identity["full_name"])
        dept = identity["department"]
        department_counts[dept] = department_counts.get(dept, 0) + 1

        assignments[agent.agent_id] = identity

    return assignments
