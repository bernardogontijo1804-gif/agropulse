from twilio.rest import Client

TWILIO_SID = "ACa149c70f4cf4ef201d5f7bce2d8cf14b"
TWILIO_TOKEN = "58180ce99b83135e07ca738ae7b7fa1a"

cliente = Client(TWILIO_SID, TWILIO_TOKEN)

mensagem = cliente.messages.create(
    from_="whatsapp:+14155238886",
    to="whatsapp:+553897344327",
    body="Teste AgroPulse! 🌾"
)

print(f"Status: {mensagem.status}")
print(f"SID: {mensagem.sid}")