import base64

with open('credentials.json','rb') as f:
    data = f.read()
print(base64.b64encode(data).decode())
