import os
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from services import parse_trip, geocode, reverse_geocode, route_options, search_places

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "database" / "smartroute.db"
DB_PATH.parent.mkdir(exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH.as_posix()}")
Session = sessionmaker(bind=engine)
Base = declarative_base()

class RouteHistory(Base):
    __tablename__ = "route_history"
    id=Column(Integer,primary_key=True); source=Column(String(240),nullable=False); destination=Column(String(240),nullable=False); vehicle=Column(String(40),nullable=False); distance=Column(Float); duration=Column(Float); fuel_cost=Column(Float); co2=Column(Float); optimization_type=Column(String(40)); created_at=Column(DateTime,default=lambda:datetime.now(timezone.utc))
class Favorite(Base):
    __tablename__ = "favorites"
    id=Column(Integer,primary_key=True); name=Column(String(100),nullable=False); location=Column(String(240),nullable=False)
class Vehicle(Base):
    __tablename__ = "vehicles"
    id=Column(Integer,primary_key=True); name=Column(String(100),nullable=False); type=Column(String(40)); fuel_type=Column(String(40)); efficiency=Column(Float); energy_price=Column(Float); emission_factor=Column(Float,default=0)
class Preference(Base):
    __tablename__ = "preferences"
    id=Column(Integer,primary_key=True); key=Column(String(80),unique=True); value=Column(Text)
Base.metadata.create_all(engine)

app=Flask(__name__)
CORS(app, resources={r"/api/*":{"origins":os.getenv("FRONTEND_ORIGIN","http://localhost:5173,http://127.0.0.1:5173").split(",")}})

def body():
    data=request.get_json(silent=True)
    if not isinstance(data,dict): raise ValueError("Send a JSON request body.")
    return data

@app.errorhandler(ValueError)
def bad_request(error): return jsonify({"error":str(error)}),400
@app.errorhandler(404)
def not_found(_): return jsonify({"error":"That API endpoint was not found."}),404
@app.errorhandler(Exception)
def internal_error(error):
    app.logger.exception("Request failed")
    return jsonify({"error":"Something went wrong. Please try again."}),500

@app.get("/api/health")
def health(): return jsonify({"status":"ok","service":"SmartRoute AI"})

@app.post("/api/nlp-route")
def nlp_route():
    text=body().get("text","")
    if not isinstance(text,str) or not text.strip(): raise ValueError("Enter a journey in your own words first.")
    return jsonify(parse_trip(text))

@app.post("/api/geocode")
def geocode_api():
    location=str(body().get("location","")).strip()
    if not location: raise ValueError("Location cannot be empty.")
    if len(location)>240: raise ValueError("Location is too long.")
    return jsonify(geocode(location))

@app.post("/api/reverse-geocode")
def reverse_geocode_api():
    data=body()
    try:
        latitude=float(data.get("latitude")); longitude=float(data.get("longitude"))
    except (TypeError, ValueError):
        raise ValueError("A valid GPS location is required.")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("A valid GPS location is required.")
    return jsonify(reverse_geocode(latitude,longitude))

@app.get("/api/search")
def search_api():
    query=str(request.args.get("q", "")).strip()
    if len(query)<2: return jsonify({"results":[]})
    if len(query)>120: raise ValueError("Search text is too long.")
    return jsonify({"results":search_places(query)})

@app.post("/api/routes")
def routes_api():
    data=body(); source=str(data.get("source","")).strip(); destination=str(data.get("destination","")).strip(); vehicle=str(data.get("vehicle","car")).lower(); mode=str(data.get("mode","balanced")).lower()
    if not source or not destination: raise ValueError("Enter both a starting point and destination.")
    if len(source)>240 or len(destination)>240: raise ValueError("Location is too long.")
    if mode not in {"balanced","fastest","shortest","cheapest","eco"}: raise ValueError("Choose a valid route preference.")
    if vehicle in {"walking", "bicycle"}: raise ValueError("This public OSRM service provides driving routes. Walking and cycling need a separate routing provider.")
    profile=None
    with Session() as db:
        if data.get("vehicle_id"):
            v=db.get(Vehicle,int(data["vehicle_id"]))
            if v: profile={"fuel_type":v.fuel_type,"efficiency":v.efficiency,"energy_price":v.energy_price,"emission_factor":v.emission_factor}
    routes,best=route_options(source,destination,vehicle,mode,profile,
                              data.get("source_coordinates"),data.get("destination_coordinates"))
    return jsonify({"routes":routes,"recommended_index":best,"traffic":"not included","provider":"OSRM"})

@app.post("/api/chat")
def route_chat():
    data=body(); message=str(data.get("message", "")).strip()
    if not message: raise ValueError("Type or say a question first.")
    mode=str(data.get("mode", "balanced")).lower()
    try:
        trip=parse_trip(message)
    except ValueError:
        trip=None
    if trip:
        if trip["vehicle"] in {"walking", "bicycle"}:
            raise ValueError("The public routing service currently provides driving routes. Try car or bike.")
        routes,best=route_options(trip["source"],trip["destination"],trip["vehicle"],mode)
        chosen=routes[best]
        cost=f"₹{chosen['cost']:.0f}" if chosen["fuel_type"]!="electric" else f"₹{chosen['cost']:.0f} charging"
        return jsonify({"reply":f"I found {len(routes)} real route option(s) from {trip['source']} to {trip['destination']}. {chosen['tag']} is {chosen['distance_km']} km and about {chosen['duration_min']} minutes, with an estimated cost of {cost}.","trip":trip,"routes":routes,"recommended_index":best})
    current=data.get("current_route") or {}; options=data.get("routes") or []
    if current:
        normalized=message.lower()
        if options and any(word in normalized for word in ("cheapest", "fastest", "eco", "shortest")):
            key=next((m for m in ("cheapest","fastest","eco","shortest") if m in normalized),None)
            field={"cheapest":"cost","fastest":"duration_seconds","eco":"co2_kg","shortest":"distance_km"}[key]
            idx=min(range(len(options)),key=lambda i:options[i].get(field,0))
            chosen=options[idx]
        else:
            chosen=current;idx=int(data.get("selected_index",0))
        return jsonify({"reply":f"For {data.get('source','your trip')} to {data.get('destination','your destination')}, this option is {chosen.get('distance_km')} km and about {chosen.get('duration_min')} minutes. Estimated cost: ₹{float(chosen.get('cost',0)):.0f}; fuel: {chosen.get('fuel_used',0)} L; operational emissions: {chosen.get('co2_kg',0)} kg CO₂. OSRM times do not include live traffic.","routes":options,"recommended_index":idx})
    return jsonify({"reply":"I can find and compare real routes. Try asking: ‘Route from CMR College to RK Beach by car.’ You can also ask about the distance, time, estimated fuel, cost, or CO₂ for a route shown on the map."})

@app.get("/api/history")
def get_history():
    with Session() as db:
        return jsonify([{"id":x.id,"source":x.source,"destination":x.destination,"vehicle":x.vehicle,"distance":x.distance,"duration":x.duration,"fuel_cost":x.fuel_cost,"co2":x.co2,"optimization_type":x.optimization_type,"created_at":x.created_at.isoformat()} for x in db.query(RouteHistory).order_by(RouteHistory.id.desc()).limit(50).all()])

@app.post("/api/history")
def save_history():
    data=body(); r=data.get("route") or {}
    if not data.get("source") or not data.get("destination") or not r: raise ValueError("A completed route is required to save a journey.")
    with Session() as db:
        item=RouteHistory(source=str(data["source"])[:240],destination=str(data["destination"])[:240],vehicle=str(data.get("vehicle","car"))[:40],distance=float(r.get("distance_km",0)),duration=float(r.get("duration_min",0)),fuel_cost=float(r.get("cost",0)),co2=float(r.get("co2_kg",0)),optimization_type=str(data.get("optimization_type","balanced"))[:40]);db.add(item);db.commit();return jsonify({"id":item.id,"message":"Journey saved"}),201

@app.get("/api/favorites")
def get_favorites():
    with Session() as db:return jsonify([{"id":x.id,"name":x.name,"location":x.location} for x in db.query(Favorite).order_by(Favorite.id.desc()).all()])
@app.post("/api/favorites")
def save_favorite():
    data=body();name=str(data.get("name","")).strip();location=str(data.get("location","")).strip()
    if not name or not location: raise ValueError("A favorite needs a name and location.")
    with Session() as db:
        item=Favorite(name=name[:100],location=location[:240]);db.add(item);db.commit();return jsonify({"id":item.id,"name":item.name,"location":item.location}),201
@app.delete("/api/favorites/<int:item_id>")
def delete_favorite(item_id):
    with Session() as db:
        item=db.get(Favorite,item_id)
        if not item:return jsonify({"error":"Favorite not found."}),404
        db.delete(item);db.commit();return jsonify({"message":"Favorite removed"})

@app.get("/api/vehicles")
def get_vehicles():
    with Session() as db:return jsonify([{"id":v.id,"name":v.name,"type":v.type,"fuel_type":v.fuel_type,"efficiency":v.efficiency,"energy_price":v.energy_price,"emission_factor":v.emission_factor} for v in db.query(Vehicle).all()])
@app.post("/api/vehicles")
def add_vehicle():
    data=body();name=str(data.get("name","")).strip();fuel=str(data.get("fuel_type","petrol")).lower()
    if not name: raise ValueError("Give this vehicle a name.")
    if fuel not in {"petrol","diesel","electric","none"}:raise ValueError("Choose petrol, diesel, electric, or none.")
    efficiency=float(data.get("efficiency",0));price=float(data.get("energy_price",0))
    if efficiency<=0 or price<0:raise ValueError("Efficiency must be above zero and price cannot be negative.")
    with Session() as db:
        v=Vehicle(name=name[:100],type=str(data.get("type","car"))[:40],fuel_type=fuel,efficiency=efficiency,energy_price=price,emission_factor=float(data.get("emission_factor",0)));db.add(v);db.commit();return jsonify({"id":v.id,"name":v.name}),201
@app.delete("/api/vehicles/<int:item_id>")
def delete_vehicle(item_id):
    with Session() as db:
        item=db.get(Vehicle,item_id)
        if not item:return jsonify({"error":"Vehicle not found."}),404
        db.delete(item);db.commit();return jsonify({"message":"Vehicle removed"})

@app.get("/api/preferences")
def get_preferences():
    with Session() as db:return jsonify({x.key:x.value for x in db.query(Preference).all()})
@app.put("/api/preferences")
def put_preferences():
    data=body()
    with Session() as db:
        for key,value in data.items():
            pref=db.query(Preference).filter_by(key=str(key)[:80]).first()
            if pref:pref.value=str(value)[:500]
            else:db.add(Preference(key=str(key)[:80],value=str(value)[:500]))
        db.commit();return jsonify({"message":"Preferences saved"})

if __name__=="__main__": app.run(host="127.0.0.1",port=int(os.getenv("PORT",5000)),debug=os.getenv("FLASK_DEBUG")=="1")
