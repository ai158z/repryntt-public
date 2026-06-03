"""
SAIGE Simulation Toolkit — Real-World Simulators for Autonomous AI Research

Gives agents (Jarvis, etc.) the ability to design experiments, run simulations,
analyze results, and iterate — all through structured tool calls.

Available simulation domains:
  1. PHYSICS / ROBOTICS  — PyBullet rigid-body simulation
  2. AGENT-BASED MODELS  — Mesa emergent-behavior modeling
  3. REINFORCEMENT LEARN  — Gymnasium RL environments
  4. DISCRETE EVENTS     — SimPy process simulation
  5. REAL-WORLD DATA     — Open-Meteo weather + NASA + seismic APIs
  6. QUANTUM PHYSICS     — NetKet quantum many-body (if available)

Design philosophy:
  - Each tool returns structured JSON so the LLM can reason over results
  - Simulations run headless (no GUI) — results are numeric + optional saved images
  - All outputs go to brain/simulation_results/ for persistence & cross-session learning
  - Agents can chain: design → run → analyze → redesign → run again
"""

import os
import sys
import json
import time
import logging
import traceback
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("simulation_toolkit")

# ──────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────
SAIGE_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = SAIGE_ROOT / "brain" / "simulation_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

def _save_result(domain: str, experiment_name: str, data: dict) -> str:
    """Save simulation results to disk. Returns the file path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in experiment_name)[:60]
    fname = f"{domain}_{safe_name}_{ts}.json"
    path = RESULTS_DIR / fname
    data["_meta"] = {
        "domain": domain,
        "experiment": experiment_name,
        "timestamp": ts,
        "file": str(path),
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return str(path)


# ======================================================================
#  1. PHYSICS & ROBOTICS — PyBullet
# ======================================================================

def sim_physics_experiment(
    experiment_name: str,
    description: str = "",
    setup_code: str = "",
    steps: int = 240,
    objects: list = None,
    gravity: float = -9.81,
    save_frames: bool = False,
) -> str:
    """
    Run a PyBullet physics simulation experiment.

    Args:
        experiment_name: Short name for this experiment (e.g. "wheelchair_ramp_test")
        description: What you're testing and why
        setup_code: Optional Python code string to customize the simulation.
                    Has access to: p (pybullet), physicsClient, RESULTS_DIR.
                    Should define a function `setup(p, client)` that returns
                    a dict of object IDs, and optionally `measure(p, client, objects, step)`
                    that returns a dict of measurements per step.
        steps: Number of simulation steps to run (at 240Hz, so 240 = 1 second)
        objects: List of object dicts to spawn. Each dict has:
                 - shape: "box" | "sphere" | "cylinder" | "plane"
                 - position: [x, y, z]
                 - size: [x, y, z] or radius
                 - mass: float (0 = static)
                 - color: [r, g, b, a] (optional)
        gravity: Gravity in m/s^2 (default: Earth = -9.81)
        save_frames: If True, save rendered frames as images (slower)

    Returns:
        JSON with simulation results, object final positions, and metrics.
    """
    try:
        import pybullet as p
        import pybullet_data
    except ImportError:
        return json.dumps({"error": "PyBullet not installed. Run: pip install pybullet"})

    result = {
        "experiment": experiment_name,
        "description": description,
        "parameters": {"steps": steps, "gravity": gravity, "num_objects": len(objects or [])},
        "objects_final": [],
        "measurements": [],
        "summary": {},
    }

    try:
        # Start headless physics server
        physicsClient = p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, gravity)

        # Load ground plane
        plane_id = p.loadURDF("plane.urdf")
        spawned = {"ground_plane": plane_id}

        # Spawn objects from declarative list
        if objects:
            for i, obj in enumerate(objects):
                shape = obj.get("shape", "box")
                pos = obj.get("position", [0, 0, 1])
                mass = obj.get("mass", 1.0)
                color = obj.get("color", [0.5, 0.5, 0.5, 1.0])
                size = obj.get("size", [0.5, 0.5, 0.5])
                name = obj.get("name", f"object_{i}")

                if shape == "box":
                    half = [s / 2 for s in (size if isinstance(size, list) else [size]*3)]
                    col_id = p.createCollisionShape(p.GEOM_BOX, halfExtents=half)
                    vis_id = p.createVisualShape(p.GEOM_BOX, halfExtents=half, rgbaColor=color)
                elif shape == "sphere":
                    r = size if isinstance(size, (int, float)) else size[0]
                    col_id = p.createCollisionShape(p.GEOM_SPHERE, radius=r)
                    vis_id = p.createVisualShape(p.GEOM_SPHERE, radius=r, rgbaColor=color)
                elif shape == "cylinder":
                    r = size[0] if isinstance(size, list) else size
                    h = size[1] if isinstance(size, list) and len(size) > 1 else 1.0
                    col_id = p.createCollisionShape(p.GEOM_CYLINDER, radius=r, height=h)
                    vis_id = p.createVisualShape(p.GEOM_CYLINDER, radius=r, length=h, rgbaColor=color)
                else:
                    continue

                body_id = p.createMultiBody(
                    baseMass=mass,
                    baseCollisionShapeIndex=col_id,
                    baseVisualShapeIndex=vis_id,
                    basePosition=pos,
                )
                spawned[name] = body_id

        # Execute custom setup code if provided
        custom_measure = None
        if setup_code:
            local_ns = {"p": p, "physicsClient": physicsClient, "spawned": spawned,
                        "RESULTS_DIR": str(RESULTS_DIR), "pybullet_data": pybullet_data}
            exec(setup_code, local_ns)
            if "setup" in local_ns:
                extra = local_ns["setup"](p, physicsClient)
                if isinstance(extra, dict):
                    spawned.update(extra)
            if "measure" in local_ns:
                custom_measure = local_ns["measure"]

        # Run simulation
        measurements = []
        for step in range(steps):
            p.stepSimulation()

            # Collect measurements every 24 steps (~10Hz)
            if step % 24 == 0:
                m = {"step": step, "time_s": round(step / 240.0, 3)}
                for name, body_id in spawned.items():
                    if name == "ground_plane":
                        continue
                    try:
                        pos, orn = p.getBasePositionAndOrientation(body_id)
                        vel, ang_vel = p.getBaseVelocity(body_id)
                        m[f"{name}_pos"] = [round(x, 4) for x in pos]
                        m[f"{name}_vel"] = [round(x, 4) for x in vel]
                    except Exception:
                        pass

                if custom_measure:
                    try:
                        custom_data = custom_measure(p, physicsClient, spawned, step)
                        if isinstance(custom_data, dict):
                            m.update(custom_data)
                    except Exception:
                        pass

                measurements.append(m)

        # Collect final state
        objects_final = []
        for name, body_id in spawned.items():
            if name == "ground_plane":
                continue
            try:
                pos, orn = p.getBasePositionAndOrientation(body_id)
                vel, _ = p.getBaseVelocity(body_id)
                objects_final.append({
                    "name": name,
                    "final_position": [round(x, 4) for x in pos],
                    "final_orientation": [round(x, 4) for x in orn],
                    "final_velocity": [round(x, 4) for x in vel],
                })
            except Exception:
                pass

        p.disconnect()

        result["objects_final"] = objects_final
        result["measurements"] = measurements
        result["summary"] = {
            "total_steps": steps,
            "sim_duration_s": round(steps / 240.0, 3),
            "num_objects": len(objects_final),
            "status": "completed",
        }

        saved = _save_result("physics", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        try:
            p.disconnect()
        except Exception:
            pass
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        return json.dumps(result, default=str)


# ======================================================================
#  2. AGENT-BASED MODELING — Mesa
# ======================================================================

def sim_agent_model(
    experiment_name: str,
    description: str = "",
    model_code: str = "",
    steps: int = 100,
    width: int = 20,
    height: int = 20,
    num_agents: int = 50,
    parameters: dict = None,
) -> str:
    """
    Run a Mesa agent-based model simulation.

    Args:
        experiment_name: Name for this experiment
        description: What you're modeling and hypothesis
        model_code: Python code defining the Mesa model.
                    Must define:
                      - A class `MyAgent(mesa.Agent)` with a `step()` method
                      - A class `MyModel(mesa.Model)` with `step()` method
                      - MyModel.__init__ should accept (width, height, num_agents, **params)
                    Has access to: mesa, numpy (as np), random
        steps: Number of model steps to simulate
        width: Grid width (if using spatial model)
        height: Grid height
        num_agents: Number of agents to create
        parameters: Dict of extra parameters passed to the model

    Returns:
        JSON with per-step data collection results and summary statistics.

    Example model_code for disease spread:
        '''
        class MyAgent(mesa.Agent):
            def __init__(self, model):
                super().__init__(model)
                self.infected = False
                self.immune = False
                self.days_infected = 0

            def step(self):
                # Move randomly
                possible = self.model.grid.get_neighborhood(self.pos, moore=True, include_center=False)
                new_pos = self.random.choice(possible)
                self.model.grid.move_agent(self, new_pos)
                # Spread disease
                if self.infected:
                    self.days_infected += 1
                    neighbors = self.model.grid.get_neighbors(self.pos, moore=True)
                    for n in neighbors:
                        if not n.infected and not n.immune and self.random.random() < 0.3:
                            n.infected = True
                    if self.days_infected > 14:
                        self.infected = False
                        self.immune = True

        class MyModel(mesa.Model):
            def __init__(self, width=20, height=20, num_agents=50, **params):
                super().__init__()
                self.grid = mesa.space.MultiGrid(width, height, True)
                for i in range(num_agents):
                    a = MyAgent(self)
                    x = self.random.randrange(width)
                    y = self.random.randrange(height)
                    self.grid.place_agent(a, (x, y))
                    if i == 0:
                        a.infected = True

            def step(self):
                self.agents.shuffle_do("step")
        '''
    """
    try:
        import mesa
    except ImportError:
        return json.dumps({"error": "Mesa not installed. Run: pip install mesa"})

    import numpy as np
    import random as _random

    result = {
        "experiment": experiment_name,
        "description": description,
        "parameters": {"steps": steps, "width": width, "height": height,
                        "num_agents": num_agents, "extra": parameters or {}},
        "step_data": [],
        "summary": {},
    }

    try:
        if not model_code.strip():
            return json.dumps({"error": "model_code is required. Define MyAgent and MyModel classes."})

        # Execute model code in isolated namespace
        ns = {"mesa": mesa, "np": np, "random": _random, "math": __import__("math")}
        exec(model_code, ns)

        if "MyModel" not in ns:
            return json.dumps({"error": "model_code must define a 'MyModel' class"})

        # Create and run model
        params = parameters or {}
        model = ns["MyModel"](width=width, height=height, num_agents=num_agents, **params)

        step_data = []
        for step_num in range(steps):
            model.step()

            # Collect agent-level stats every 10 steps
            if step_num % max(1, steps // 20) == 0:
                snapshot = {"step": step_num}

                # Try to collect common agent attributes
                agents = list(model.agents) if hasattr(model, 'agents') else []
                if agents:
                    # Dynamically detect numeric/boolean attributes
                    sample = agents[0]
                    for attr_name in vars(sample):
                        if attr_name.startswith("_") or attr_name in ("unique_id", "model", "pos", "random"):
                            continue
                        vals = []
                        for a in agents:
                            v = getattr(a, attr_name, None)
                            if isinstance(v, (int, float)):
                                vals.append(v)
                            elif isinstance(v, bool):
                                vals.append(int(v))
                        if vals:
                            snapshot[f"{attr_name}_mean"] = round(np.mean(vals), 4)
                            snapshot[f"{attr_name}_sum"] = round(sum(vals), 4)
                            snapshot[f"{attr_name}_min"] = round(min(vals), 4)
                            snapshot[f"{attr_name}_max"] = round(max(vals), 4)

                step_data.append(snapshot)

        # Final summary
        agents = list(model.agents) if hasattr(model, 'agents') else []
        summary_attrs = {}
        if agents:
            sample = agents[0]
            for attr_name in vars(sample):
                if attr_name.startswith("_") or attr_name in ("unique_id", "model", "pos", "random"):
                    continue
                vals = []
                for a in agents:
                    v = getattr(a, attr_name, None)
                    if isinstance(v, (int, float, bool)):
                        vals.append(float(v))
                if vals:
                    summary_attrs[attr_name] = {
                        "mean": round(np.mean(vals), 4),
                        "std": round(np.std(vals), 4),
                        "min": round(min(vals), 4),
                        "max": round(max(vals), 4),
                        "sum": round(sum(vals), 4),
                    }

        result["step_data"] = step_data
        result["summary"] = {
            "status": "completed",
            "total_steps": steps,
            "surviving_agents": len(agents),
            "agent_attributes": summary_attrs,
        }

        saved = _save_result("agent_model", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        return json.dumps(result, default=str)


# ======================================================================
#  3. REINFORCEMENT LEARNING — Gymnasium
# ======================================================================

def sim_rl_experiment(
    experiment_name: str,
    description: str = "",
    env_id: str = "CartPole-v1",
    episodes: int = 10,
    max_steps_per_episode: int = 500,
    agent_code: str = "",
    render: bool = False,
) -> str:
    """
    Run a Gymnasium reinforcement learning experiment.

    Args:
        experiment_name: Name for this experiment
        description: What you're testing
        env_id: Gymnasium environment ID. Common ones:
                - "CartPole-v1" (balance a pole on a cart)
                - "MountainCar-v0" (drive car up hill)
                - "Acrobot-v1" (swing up a double pendulum)
                - "LunarLander-v3" (land a spacecraft)
                - "Pendulum-v1" (swing up and balance)
                - "BipedalWalker-v3" (2D walking robot)
        episodes: Number of episodes to run
        max_steps_per_episode: Max steps before episode ends
        agent_code: Python code defining the agent's policy.
                    Must define: `select_action(observation, env)` → action
                    Has access to: numpy (np), random, env (the gym environment)
                    If empty, uses random actions.

    Returns:
        JSON with per-episode rewards, steps, and summary statistics.
    """
    try:
        import gymnasium as gym
    except ImportError:
        return json.dumps({"error": "Gymnasium not installed. Run: pip install gymnasium"})

    import numpy as np

    result = {
        "experiment": experiment_name,
        "description": description,
        "parameters": {"env_id": env_id, "episodes": episodes,
                        "max_steps": max_steps_per_episode},
        "episodes_data": [],
        "summary": {},
    }

    try:
        env = gym.make(env_id)

        # Build agent policy
        select_action = None
        if agent_code and agent_code.strip():
            ns = {"np": np, "random": __import__("random"), "env": env, "gym": gym}
            exec(agent_code, ns)
            if "select_action" in ns:
                select_action = ns["select_action"]

        if select_action is None:
            select_action = lambda obs, env: env.action_space.sample()

        episode_rewards = []
        episode_lengths = []
        episodes_data = []

        for ep in range(episodes):
            obs, info = env.reset()
            total_reward = 0
            ep_steps = 0

            for step in range(max_steps_per_episode):
                action = select_action(obs, env)
                obs, reward, terminated, truncated, info = env.step(action)
                total_reward += reward
                ep_steps += 1
                if terminated or truncated:
                    break

            episode_rewards.append(total_reward)
            episode_lengths.append(ep_steps)
            episodes_data.append({
                "episode": ep,
                "total_reward": round(total_reward, 4),
                "steps": ep_steps,
                "terminated": terminated if 'terminated' in dir() else True,
            })

        env.close()

        result["episodes_data"] = episodes_data
        result["summary"] = {
            "status": "completed",
            "env_id": env_id,
            "observation_space": str(env.observation_space),
            "action_space": str(env.action_space),
            "mean_reward": round(np.mean(episode_rewards), 4),
            "std_reward": round(np.std(episode_rewards), 4),
            "max_reward": round(max(episode_rewards), 4),
            "min_reward": round(min(episode_rewards), 4),
            "mean_length": round(np.mean(episode_lengths), 2),
            "total_episodes": episodes,
        }

        saved = _save_result("rl", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        try:
            env.close()
        except Exception:
            pass
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        return json.dumps(result, default=str)


# ======================================================================
#  4. DISCRETE EVENT SIMULATION — SimPy
# ======================================================================

def sim_discrete_event(
    experiment_name: str,
    description: str = "",
    model_code: str = "",
    sim_duration: float = 100.0,
    parameters: dict = None,
) -> str:
    """
    Run a SimPy discrete-event simulation.

    Args:
        experiment_name: Name for this experiment
        description: What real-world process you're modeling
        model_code: Python code defining the simulation.
                    Must define: `run_simulation(env, results, params)` 
                    where env is simpy.Environment, results is a dict to fill,
                    and params is the parameters dict.
                    Has access to: simpy, random, numpy (np), math
        sim_duration: How long to run the simulation (simulation time units)
        parameters: Dict of model parameters

    Returns:
        JSON with simulation results and metrics.

    Example model_code for hospital ER:
        '''
        def patient(env, name, er, treatment_time, results):
            arrive = env.now
            with er.request() as req:
                yield req
                wait = env.now - arrive
                results["wait_times"].append(wait)
                yield env.timeout(treatment_time)
                results["treated"] += 1

        def patient_arrivals(env, er, params, results):
            i = 0
            while True:
                yield env.timeout(random.expovariate(1.0 / params.get("mean_arrival", 5)))
                treatment = random.expovariate(1.0 / params.get("mean_treatment", 10))
                env.process(patient(env, f"Patient_{i}", er, treatment, results))
                i += 1
                results["total_arrived"] = i

        def run_simulation(env, results, params):
            results["wait_times"] = []
            results["treated"] = 0
            results["total_arrived"] = 0
            num_doctors = params.get("num_doctors", 3)
            er = simpy.Resource(env, capacity=num_doctors)
            env.process(patient_arrivals(env, er, params, results))
        '''
    """
    try:
        import simpy
    except ImportError:
        return json.dumps({"error": "SimPy not installed. Run: pip install simpy"})

    import numpy as np
    import random as _random
    import math

    result = {
        "experiment": experiment_name,
        "description": description,
        "parameters": {"sim_duration": sim_duration, "extra": parameters or {}},
        "results": {},
        "summary": {},
    }

    try:
        if not model_code.strip():
            return json.dumps({"error": "model_code is required. Define a run_simulation(env, results, params) function."})

        ns = {"simpy": simpy, "random": _random, "np": np, "math": math}
        exec(model_code, ns)

        if "run_simulation" not in ns:
            return json.dumps({"error": "model_code must define 'run_simulation(env, results, params)'"})

        env = simpy.Environment()
        sim_results = {}
        params = parameters or {}

        ns["run_simulation"](env, sim_results, params)
        env.run(until=sim_duration)

        # Post-process results — compute stats for any list values
        processed = {}
        for key, val in sim_results.items():
            if isinstance(val, list) and val and isinstance(val[0], (int, float)):
                processed[key] = {
                    "count": len(val),
                    "mean": round(np.mean(val), 4),
                    "std": round(np.std(val), 4),
                    "min": round(min(val), 4),
                    "max": round(max(val), 4),
                    "median": round(np.median(val), 4),
                    "p95": round(np.percentile(val, 95), 4),
                }
            elif isinstance(val, (int, float)):
                processed[key] = val
            elif isinstance(val, list):
                processed[key] = f"[{len(val)} items]"
            else:
                processed[key] = str(val)

        result["results"] = processed
        result["summary"] = {
            "status": "completed",
            "sim_duration": sim_duration,
            "sim_time_reached": env.now,
        }

        saved = _save_result("discrete_event", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        return json.dumps(result, default=str)


# ======================================================================
#  5. REAL-WORLD DATA — Weather, Earthquakes, Climate
# ======================================================================

def sim_fetch_weather_data(
    latitude: float,
    longitude: float,
    days: int = 7,
    variables: list = None,
    experiment_name: str = "weather_analysis",
) -> str:
    """
    Fetch real-world weather data from Open-Meteo (free, no API key).

    Args:
        latitude: Location latitude (e.g. 40.7128 for NYC)
        longitude: Location longitude (e.g. -74.0060 for NYC)
        days: Forecast days (1-16)
        variables: Weather variables to fetch. Options:
                   temperature_2m, relative_humidity_2m, precipitation,
                   wind_speed_10m, wind_direction_10m, cloud_cover,
                   surface_pressure, visibility, uv_index
                   Default: temperature_2m, precipitation, wind_speed_10m
        experiment_name: Name for saving results

    Returns:
        JSON with hourly weather data and statistics.
    """
    import urllib.request
    import numpy as np

    if not variables:
        variables = ["temperature_2m", "precipitation", "wind_speed_10m"]

    result = {
        "experiment": experiment_name,
        "location": {"latitude": latitude, "longitude": longitude},
        "parameters": {"days": days, "variables": variables},
        "data": {},
        "summary": {},
    }

    try:
        vars_str = ",".join(variables)
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={latitude}&longitude={longitude}"
            f"&hourly={vars_str}"
            f"&forecast_days={days}"
            f"&timezone=auto"
        )

        req = urllib.request.Request(url, headers={"User-Agent": "SAIGE-AI/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])

        processed = {"times": times}
        stats = {}

        for var in variables:
            values = hourly.get(var, [])
            processed[var] = values
            if values:
                numeric = [v for v in values if v is not None]
                if numeric:
                    stats[var] = {
                        "mean": round(np.mean(numeric), 2),
                        "min": round(min(numeric), 2),
                        "max": round(max(numeric), 2),
                        "std": round(np.std(numeric), 2),
                    }

        result["data"] = processed
        result["summary"] = {
            "status": "completed",
            "data_points": len(times),
            "statistics": stats,
            "timezone": data.get("timezone", "unknown"),
            "elevation_m": data.get("elevation"),
        }

        saved = _save_result("weather", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        result["error"] = str(e)
        return json.dumps(result, default=str)


def sim_fetch_earthquake_data(
    min_magnitude: float = 4.0,
    days: int = 7,
    experiment_name: str = "seismic_analysis",
) -> str:
    """
    Fetch recent earthquake data from USGS (free, no API key).

    Args:
        min_magnitude: Minimum magnitude to include (1.0-9.0)
        days: How many days back to look (1-30)
        experiment_name: Name for saving results

    Returns:
        JSON with earthquake events and statistical analysis.
    """
    import urllib.request
    import numpy as np
    from datetime import timedelta

    result = {
        "experiment": experiment_name,
        "parameters": {"min_magnitude": min_magnitude, "days": days},
        "earthquakes": [],
        "summary": {},
    }

    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        url = (
            f"https://earthquake.usgs.gov/fdsnws/event/1/query?"
            f"format=geojson"
            f"&starttime={start.strftime('%Y-%m-%d')}"
            f"&endtime={end.strftime('%Y-%m-%d')}"
            f"&minmagnitude={min_magnitude}"
            f"&orderby=magnitude"
        )

        req = urllib.request.Request(url, headers={"User-Agent": "SAIGE-AI/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        features = data.get("features", [])
        earthquakes = []
        magnitudes = []

        for f in features[:100]:  # Limit to 100
            props = f.get("properties", {})
            coords = f.get("geometry", {}).get("coordinates", [0, 0, 0])
            mag = props.get("mag", 0)
            magnitudes.append(mag)
            earthquakes.append({
                "magnitude": mag,
                "place": props.get("place", "unknown"),
                "time": props.get("time"),
                "longitude": coords[0],
                "latitude": coords[1],
                "depth_km": coords[2],
                "tsunami": props.get("tsunami", 0),
                "type": props.get("type", "earthquake"),
            })

        result["earthquakes"] = earthquakes
        result["summary"] = {
            "status": "completed",
            "total_events": len(earthquakes),
            "total_in_period": data.get("metadata", {}).get("count", len(earthquakes)),
            "magnitude_stats": {
                "mean": round(np.mean(magnitudes), 2) if magnitudes else 0,
                "max": round(max(magnitudes), 2) if magnitudes else 0,
                "min": round(min(magnitudes), 2) if magnitudes else 0,
            } if magnitudes else {},
            "period": f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}",
        }

        saved = _save_result("earthquake", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        result["error"] = str(e)
        return json.dumps(result, default=str)


# ======================================================================
#  6. QUANTUM PHYSICS — NetKet (optional)
# ======================================================================

def sim_quantum_experiment(
    experiment_name: str,
    description: str = "",
    model_code: str = "",
    parameters: dict = None,
) -> str:
    """
    Run a NetKet quantum many-body simulation.

    Args:
        experiment_name: Name for this experiment
        description: What quantum system you're studying
        model_code: Python code defining the quantum simulation.
                    Must define: `run_quantum(results, params)` that populates
                    the results dict with findings.
                    Has access to: netket (nk), numpy (np), jax (if available)
        parameters: Dict of model parameters

    Returns:
        JSON with quantum simulation results.

    Example model_code for 1D Ising model ground state:
        '''
        def run_quantum(results, params):
            import netket as nk
            # 1D lattice with 10 spins
            n_sites = params.get("n_sites", 10)
            g = nk.graph.Hypercube(length=n_sites, n_dim=1, pbc=True)
            hi = nk.hilbert.Spin(s=0.5, N=g.n_nodes)

            # Transverse-field Ising Hamiltonian
            J = params.get("J", 1.0)
            h_field = params.get("h", 0.5)
            ha = nk.operator.Ising(hilbert=hi, graph=g, h=h_field, J=J)

            # Variational Monte Carlo with RBM
            ma = nk.models.RBM(alpha=1, param_dtype=complex)
            sa = nk.sampler.MetropolisLocal(hi)
            vs = nk.vqs.MCState(sa, ma, n_samples=512)
            op = nk.optimizer.Sgd(learning_rate=0.01)
            sr = nk.optimizer.SR(diag_shift=0.01)
            gs = nk.driver.VMC(ha, op, variational_state=vs, preconditioner=sr)

            # Run optimization
            n_iter = params.get("n_iter", 100)
            log = nk.logging.RuntimeLog()
            gs.run(n_iter=n_iter, out=log)

            # Extract results
            results["ground_state_energy"] = float(vs.expect(ha).mean.real)
            results["energy_variance"] = float(vs.expect(ha).variance.real)
            results["n_sites"] = n_sites
            results["n_parameters"] = vs.n_parameters
        '''
    """
    try:
        import netket as nk
    except ImportError:
        return json.dumps({
            "error": "NetKet not installed or not available on this platform.",
            "suggestion": "Try: pip install netket. Requires JAX which may need special setup on ARM64.",
            "alternative": "You can still run quantum-inspired algorithms using numpy directly."
        })

    import numpy as np

    result = {
        "experiment": experiment_name,
        "description": description,
        "parameters": parameters or {},
        "results": {},
        "summary": {},
    }

    try:
        if not model_code.strip():
            return json.dumps({"error": "model_code is required. Define run_quantum(results, params)."})

        ns = {"nk": nk, "netket": nk, "np": np}
        try:
            import jax
            ns["jax"] = jax
        except ImportError:
            pass

        exec(model_code, ns)

        if "run_quantum" not in ns:
            return json.dumps({"error": "model_code must define 'run_quantum(results, params)'"})

        quantum_results = {}
        ns["run_quantum"](quantum_results, parameters or {})

        # Convert any non-serializable types
        processed = {}
        for k, v in quantum_results.items():
            try:
                json.dumps(v)
                processed[k] = v
            except (TypeError, ValueError):
                processed[k] = str(v)

        result["results"] = processed
        result["summary"] = {"status": "completed"}

        saved = _save_result("quantum", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        return json.dumps(result, default=str)


# ======================================================================
#  7. GENERAL PURPOSE — Run any scientific Python experiment
# ======================================================================

def sim_custom_experiment(
    experiment_name: str,
    description: str = "",
    code: str = "",
    parameters: dict = None,
) -> str:
    """
    Run a custom scientific Python experiment. Flexible catch-all for experiments
    that don't fit the specialized tools above.

    Args:
        experiment_name: Name for this experiment
        description: What you're investigating and why
        code: Python code to execute. Must define:
              `run_experiment(results, params)` that populates results dict.
              Has access to: numpy (np), math, random, json, os, datetime,
              scipy (if installed), sklearn (if installed)
        parameters: Dict of experiment parameters

    Returns:
        JSON with experiment results.
    """
    import numpy as np
    import math
    import random as _random

    result = {
        "experiment": experiment_name,
        "description": description,
        "parameters": parameters or {},
        "results": {},
        "summary": {},
    }

    try:
        if not code.strip():
            return json.dumps({"error": "code is required. Define run_experiment(results, params)."})

        ns = {
            "np": np, "numpy": np, "math": math, "random": _random,
            "json": json, "os": os, "datetime": datetime,
        }

        # Optionally import scipy and sklearn if available
        for lib_name in ["scipy", "sklearn"]:
            try:
                ns[lib_name] = __import__(lib_name)
            except ImportError:
                pass

        exec(code, ns)

        if "run_experiment" not in ns:
            return json.dumps({"error": "code must define 'run_experiment(results, params)'"})

        exp_results = {}
        ns["run_experiment"](exp_results, parameters or {})

        # Serialize
        processed = {}
        for k, v in exp_results.items():
            try:
                json.dumps(v)
                processed[k] = v
            except (TypeError, ValueError):
                if hasattr(v, 'tolist'):
                    processed[k] = v.tolist()
                else:
                    processed[k] = str(v)

        result["results"] = processed
        result["summary"] = {"status": "completed"}

        saved = _save_result("custom", experiment_name, result)
        result["saved_to"] = saved
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        return json.dumps(result, default=str)


# ======================================================================
#  8. EXPERIMENT MANAGER — List, compare, and learn from past experiments
# ======================================================================

def sim_list_experiments(domain: str = "", limit: int = 20) -> str:
    """
    List past simulation experiments with their results summaries.

    Args:
        domain: Filter by domain ("physics", "agent_model", "rl", "discrete_event",
                "weather", "earthquake", "quantum", "custom"). Empty = all.
        limit: Max results to return

    Returns:
        JSON list of past experiments with key metrics.
    """
    experiments = []

    try:
        files = sorted(RESULTS_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)

        for f in files[:200]:
            if domain and not f.name.startswith(domain):
                continue

            try:
                with open(f) as fh:
                    data = json.load(fh)
                meta = data.get("_meta", {})
                summary = data.get("summary", {})
                experiments.append({
                    "file": f.name,
                    "domain": meta.get("domain", "unknown"),
                    "experiment": meta.get("experiment", f.stem),
                    "timestamp": meta.get("timestamp", ""),
                    "status": summary.get("status", "unknown"),
                    "summary_keys": list(summary.keys()),
                })
            except Exception:
                continue

            if len(experiments) >= limit:
                break

    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({
        "total_experiments": len(experiments),
        "experiments": experiments,
        "results_dir": str(RESULTS_DIR),
    }, indent=2)


def sim_read_experiment(filename: str) -> str:
    """
    Read the full results of a past experiment.

    Args:
        filename: Name of the experiment file (from sim_list_experiments)

    Returns:
        Full JSON results of the experiment.
    """
    try:
        path = RESULTS_DIR / filename
        if not path.exists():
            # Try partial match
            matches = list(RESULTS_DIR.glob(f"*{filename}*"))
            if matches:
                path = matches[0]
            else:
                return json.dumps({"error": f"Experiment file '{filename}' not found"})

        with open(path) as f:
            data = json.load(f)
        return json.dumps(data, indent=2, default=str)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ======================================================================
#  TOOL REGISTRY — All simulation tools for brain_system registration
# ======================================================================

SIMULATION_TOOLS = {
    # Physics
    "sim_physics_experiment": sim_physics_experiment,
    # Agent-based
    "sim_agent_model": sim_agent_model,
    # Reinforcement learning
    "sim_rl_experiment": sim_rl_experiment,
    # Discrete events
    "sim_discrete_event": sim_discrete_event,
    # Real-world data
    "sim_fetch_weather_data": sim_fetch_weather_data,
    "sim_fetch_earthquake_data": sim_fetch_earthquake_data,
    # Quantum
    "sim_quantum_experiment": sim_quantum_experiment,
    # General purpose
    "sim_custom_experiment": sim_custom_experiment,
    # Experiment management
    "sim_list_experiments": sim_list_experiments,
    "sim_read_experiment": sim_read_experiment,
}
