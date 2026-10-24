from flask import Flask, jsonify, request

app = Flask(__name__)

@app.route("/api/peladas", methods=["GET"])
def get_peladas():
    return jsonify({"message": "Bem-vindo à Pelada de Ti Fonso!"})

# Necessário para o Vercel reconhecer o app Flask
if __name__ == "__main__":
    app.run(debug=True)