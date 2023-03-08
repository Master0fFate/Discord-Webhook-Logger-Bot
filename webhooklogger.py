import discord
import asyncio
import aiohttp
import discord_webhook
import datetime

TOKEN = 'bot token'
WEBHOOK_URL = 'webhook url'

client = discord.Client(intents=discord.Intents.all())

async def send_to_webhook(message):
    async with aiohttp.ClientSession() as session:
        author_id = message.author.id
        author_name = message.author.name
        author_nick = message.author.nick
        #timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') -- {timestamp}
        content = f"```js\nUser ID: {author_id} \nUser Name: {author_name} [{author_nick}] \nServer: {message.guild} [{message.guild.id}] \nChannel: {message.channel.name} [{message.channel.id}] \n```**Content:**\n {message.content}"
        if message.attachments:
            for i, attachment in enumerate(message.attachments):
                content += f"\n**Attachment {i+1}:** {attachment.url}"
        
        async with session.post(WEBHOOK_URL, json={"content": content}):
            pass



@client.event
async def on_ready():
    print('WEBHOOK LOGGER')
    print('github.com/Master0fFate')
    print(f'Logged in as {client.user.name} ({client.user.id})')

@client.event
async def on_message(message):
    # Ignore DMs
    if message.guild is None:
        return
    
    # Ignore messages sent by bots
    if message.author.bot:
        return
    await send_to_webhook(message)

client.run(TOKEN)