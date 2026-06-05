"""Import the 3 live Indeed jobs into the DB."""
import os
os.environ["JOB_AGENT_DB"] = "job_agent.db"
from src.db.connection import get_connection
from src.discovery.indeed import import_jobs

conn = get_connection("job_agent.db")

jobs = [
    {
        "url": "https://to.indeed.com/aa6xxjb4prrz",
        "company": "Amazon.com (Fauna Robotics)",
        "title": "Senior ML Engineer, Fauna",
        "location": "New York, NY",
        "description": """Senior ML Engineer, Fauna — Amazon.com — New York, NY
Compensation: $184,900 - $250,200 a year

We are seeking a Machine Learning Engineer to work alongside research scientists to
train, evaluate, and deploy models that make robots move, perceive, and act in the
real world. This is a hands-on ML role: train policies, debug convergence, run
experiments in simulation, and push models onto hardware.

Key Responsibilities:
- Train and iterate on neural network policies for locomotion, manipulation, navigation
  using reinforcement and supervised learning
- Design and run experiments in simulation (Isaac Lab, MuJoCo) and transfer to hardware
- Debug training runs: diagnosing convergence failures, reward shaping, sim-to-real gaps
- Optimize models for edge deployment (NVIDIA Jetson) with strict latency constraints
- Build MLOps infrastructure: experiment tracking, model versioning, eval pipelines

Basic Qualifications:
- 5+ years professional ML engineering with direct model training responsibilities
- Master's or PhD in Computer Science, Machine Learning, Robotics, or related
- Strong Python and deep proficiency with PyTorch
- Experience designing, training, debugging non-trivial model architectures

Preferred:
- Reinforcement learning or imitation learning experience
- Docker, Kubernetes
- Model optimization for edge (quantization, TensorRT, ONNX)
- Physics simulation (Isaac Lab, MuJoCo, PyBullet)
- MLflow, Weights & Biases, Kubeflow, Ray
""",
        "platform": "custom",
        "has_screener": False,
    },
    {
        "url": "https://to.indeed.com/aa4ddn6s44yw",
        "company": "Deloitte (SFL Scientific)",
        "title": "Data Scientist Engineer",
        "location": "New York, NY",
        "description": """Data Scientist Engineer — Deloitte SFL Scientific — New York, NY
Compensation: $95,600 - $188,400 a year

Join SFL Scientific, a Deloitte Business, working on novel data science and AI
projects: cancer detection, drug discovery, autonomous systems, agentic solutions,
consumer product innovation.

Responsibilities:
- Guide clients in AI strategy and development, EDA, model building, production deployment
- Research and implement novel ML approaches, training, solution design, hardware optimization
- Validate AI models via code reviews, unit and integration tests
- Collaborate with data engineers, data scientists, project managers on AI delivery

Required Qualifications:
- Master's or PhD in STEM field (Data Science, Computer Science, Engineering)
- 2+ years AI/ML development in Python, PyTorch
- 2+ years deep learning (CNNs, RNNs, GANs), model tuning, production validation
- 2+ years deploying ML with Kubernetes, Docker, MLflow
- 2+ years cloud (AWS, Azure, GCP) for ML workloads

Preferred:
- 1+ year LLM/GenAI: RAG solutions, agent-based tools, LangChain, LangGraph, MCP
- AWS Sagemaker or AWS ML Studio
- Consulting / client-facing environment experience
""",
        "platform": "workday",
        "has_screener": True,
    },
    {
        "url": "https://to.indeed.com/aafl64vtpjp4",
        "company": "ThesisGrid Capital",
        "title": "Machine Learning Engineer",
        "location": "New York, NY",
        "description": """Machine Learning Engineer — ThesisGrid Capital — New York, NY
Compensation: $70,000 - $120,000 a year

ThesisGrid Capital is building the next-generation AI operating system for
institutional research teams. Work on NLP, LLM, and retrieval systems powering
investment research.

Responsibilities:
- Design and implement production-grade ML pipelines and infrastructure
- Develop and fine-tune NLP models for financial text understanding
- Build retrieval and ranking systems using vector databases
- Optimize model performance, latency, and scalability
- Implement MLOps best practices: monitoring, testing, CI/CD
- Collaborate with research scientists to productionize models

Qualifications:
- Master's or PhD in Computer Science, Machine Learning
- 3+ years building production ML systems
- Strong Python, PyTorch, TensorFlow
- NLP, LLMs, transformer architectures experience
- AWS or GCP, Docker, Kubernetes
- Vector databases and retrieval systems a plus
""",
        "platform": "greenhouse",
        "has_screener": False,
    },
]

summary = import_jobs(conn, jobs, source="indeed")
conn.close()

print(f"Imported: {summary['added']} new jobs")
print(f"Skipped duplicates: {summary['skipped_duplicate']}")
print(f"New app IDs: {summary['new_app_ids']}")
