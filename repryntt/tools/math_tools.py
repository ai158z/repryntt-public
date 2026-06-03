"""
math_tools.py — Mathematical tools extracted from BrainSystem monolith.

All 8 tools delegate to scripts.mathematical_tools (lives in SAIGE/scripts/).
The SAIGE scripts directory must be on sys.path (bootstrap.py handles this).
"""

import json
import logging

logger = logging.getLogger("repryntt.tools.math_tools")

_math_tools_cache = None


def _get_tools():
    """Lazy-load the mathematical tools suite from SAIGE scripts."""
    global _math_tools_cache
    if _math_tools_cache is not None:
        return _math_tools_cache
    try:
        from scripts.mathematical_tools import initialize_mathematical_tools
        _math_tools_cache = initialize_mathematical_tools()
        return _math_tools_cache
    except ImportError:
        logger.warning("scripts.mathematical_tools not on sys.path — math tools unavailable")
        return None
    except Exception as e:
        logger.warning(f"mathematical_tools initialization failed: {e}")
        return None


def compute_zeta_function(s_value: str = "2+0j", precision: int = 50, **kw) -> dict:
    """Compute Riemann zeta function at a complex point.

    Parameters:
        s_value: Complex number as string (e.g. '0.5+14.134j')
        precision: Decimal precision for computation
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        s = complex(s_value)
    except ValueError:
        return {"error": f"Invalid complex number format: {s_value}"}
    try:
        result = tools.compute_zeta_function(s, int(precision))
        if result is None:
            return {"error": "Failed to compute zeta function"}
        return {
            "s_value": s_value,
            "zeta_result": f"{result.real:.{precision}f}{'+' if result.imag >= 0 else ''}{result.imag:.{precision}f}j",
            "real_part": float(result.real),
            "imaginary_part": float(result.imag),
            "magnitude": abs(result),
            "precision": int(precision),
            "computation_method": "mpmath high-precision arithmetic",
        }
    except Exception as e:
        logger.error(f"Error computing zeta: {e}")
        return {"error": str(e)}


def analyze_zeta_zeros(num_zeros: int = 10, **kw) -> dict:
    """Analyze the first N non-trivial zeros of the Riemann zeta function.

    Parameters:
        num_zeros: Number of zeros to analyze (default 10)
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        import numpy as np
        zeros = tools.analyze_zeta_zeros(int(num_zeros))
        if not zeros:
            return {"error": "Failed to compute zeta zeros"}
        imag_parts = [z.imag for z in zeros]
        return {
            "num_zeros_analyzed": len(zeros),
            "zeros": [{"index": i + 1,
                        "value": f"{z.real:.10f}{'+' if z.imag >= 0 else ''}{z.imag:.10f}j"}
                       for i, z in enumerate(zeros)],
            "imaginary_parts": imag_parts,
            "critical_line_verified": all(abs(z.real - 0.5) < 1e-10 for z in zeros),
            "spacing_analysis": {
                "min_spacing": float(min(np.diff(imag_parts))) if len(imag_parts) > 1 else None,
                "max_spacing": float(max(np.diff(imag_parts))) if len(imag_parts) > 1 else None,
                "average_spacing": float(np.mean(np.diff(imag_parts))) if len(imag_parts) > 1 else None,
            },
            "method": "Newton's method with Gram point initialization",
        }
    except Exception as e:
        logger.error(f"Error analyzing zeta zeros: {e}")
        return {"error": str(e)}


def symbolic_manipulation(expression: str = "", **kw) -> dict:
    """Perform symbolic mathematical operations.

    Parameters:
        expression: Mathematical expression to manipulate
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        return tools.symbolic_manipulation(expression)
    except Exception as e:
        logger.error(f"Error in symbolic manipulation: {e}")
        return {"error": str(e)}


def numerical_analysis(function: str = "x**2", domain_start: float = -1.0,
                       domain_end: float = 1.0, num_points: int = 100, **kw) -> dict:
    """Perform numerical analysis of mathematical functions.

    Parameters:
        function: Function expression (e.g. 'x**2', 'sin(x)')
        domain_start: Start of analysis domain
        domain_end: End of analysis domain
        num_points: Number of points to analyze
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        return tools.numerical_analysis(function, (float(domain_start), float(domain_end)), int(num_points))
    except Exception as e:
        logger.error(f"Error in numerical analysis: {e}")
        return {"error": str(e)}


def statistical_analysis(data_points: str = "", **kw) -> dict:
    """Perform statistical analysis on numerical data.

    Parameters:
        data_points: Comma-separated list of numerical values
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        data = [float(x.strip()) for x in data_points.split(",")]
    except ValueError:
        return {"error": f"Invalid data format. Expected comma-separated numbers: {data_points}"}
    if len(data) < 2:
        return {"error": "Need at least 2 data points for statistical analysis"}
    try:
        return tools.statistical_analysis(data)
    except Exception as e:
        logger.error(f"Error in statistical analysis: {e}")
        return {"error": str(e)}


def pattern_recognition(sequence: str = "", methods: str = "fft,autocorr", **kw) -> dict:
    """Apply pattern recognition techniques to mathematical sequences.

    Parameters:
        sequence: Comma-separated sequence of numbers
        methods: Comma-separated list of methods (fft, autocorr, regression)
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        data = [float(x.strip()) for x in sequence.split(",")]
    except ValueError:
        return {"error": f"Invalid sequence format. Expected comma-separated numbers: {sequence}"}
    if len(data) < 10:
        return {"error": "Need at least 10 data points for meaningful pattern recognition"}
    method_list = [m.strip() for m in methods.split(",")]
    try:
        result = tools.pattern_recognition(data, method_list)
        result["sequence_length"] = len(data)
        result["methods_used"] = method_list
        return result
    except Exception as e:
        logger.error(f"Error in pattern recognition: {e}")
        return {"error": str(e)}


def access_mathematical_databases(query: str = "", database: str = "oeis", **kw) -> dict:
    """Access mathematical databases and resources.

    Parameters:
        query: Search query
        database: Database to search ('oeis', 'lmfdb')
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        return tools.access_mathematical_databases(query, database)
    except Exception as e:
        logger.error(f"Error accessing mathematical databases: {e}")
        return {"error": str(e)}


def mathematical_visualization(data_type: str = "", parameters: str = "{}", **kw) -> dict:
    """Generate mathematical visualizations.

    Parameters:
        data_type: Type of visualization ('zeta_zeros', 'function_graph')
        parameters: JSON string with visualization parameters
    """
    tools = _get_tools()
    if not tools:
        return {"error": "Mathematical tools not available"}
    try:
        params = json.loads(parameters) if isinstance(parameters, str) else parameters
    except json.JSONDecodeError:
        params = {}
    params["data_type"] = data_type
    try:
        return tools.generate_mathematical_visualization(params, data_type)
    except Exception as e:
        logger.error(f"Error generating visualization: {e}")
        return {"error": str(e)}
