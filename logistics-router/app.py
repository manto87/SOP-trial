from flask import Flask, render_template, request, jsonify
from route_engine import calculate_all, sea_route, air_route, truck_route, PORTS

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ports")
def api_ports():
    return jsonify(PORTS)


def _normalize_mode(m):
    """Map route_engine keys to stable API keys for the frontend."""
    if not m or not m.get("ok"):
        return {"error": "unavailable", "waypoints": []}
    return {
        "distance_km": m.get("km"),
        "transit_days": m.get("transit_days"),
        "co2_kg_per_tonne": m.get("co2_per_tonne"),
        "co2_total_kg": m.get("total_co2_kg"),
        "cost_usd": m.get("total_cost_usd"),
        "via": m.get("via"),
        "note": m.get("note"),
        "waypoints": m.get("waypoints", []),
    }


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    data = request.get_json(force=True)
    try:
        olat = float(data["olat"])
        olng = float(data["olng"])
        dlat = float(data["dlat"])
        dlng = float(data["dlng"])
        cargo_tonnes = float(data.get("cargo_tonnes", 1.0))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid input: {e}"}), 400

    try:
        raw = calculate_all(olat, olng, dlat, dlng, cargo_tonnes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = {
        "sea":   _normalize_mode(raw.get("sea")),
        "air":   _normalize_mode(raw.get("air")),
        "truck": _normalize_mode(raw.get("truck")),
        "cargo_tonnes": cargo_tonnes,
        "origin": raw.get("origin"),
        "destination": raw.get("destination"),
    }
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
