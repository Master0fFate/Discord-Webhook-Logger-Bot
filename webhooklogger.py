import discord
import asyncio
import aiohttp
import datetime
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('webhook_logger')

TOKEN = 'TOKEN'
WEBHOOK_URL = 'WEBHOOK_URL'

if not TOKEN or not WEBHOOK_URL:
    logger.error("Missing required variables. Please set DISCORD_TOKEN and WEBHOOK_URL")
    exit(1)

# Setup client
intents = discord.Intents.all()
client = discord.Client(intents=intents)

async def send_to_webhook(message):
    """Send message details to the webhook."""
    try:
        async with aiohttp.ClientSession() as session:
            author_id = message.author.id
            author_name = message.author.name
            author_nick = message.author.nick or "No nickname"  # Handle None case
            timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            
            content = (f"```js\n"
                      f"User ID: {author_id} \n"
                      f"User Name: {author_name} [{author_nick}] \n"
                      f"Server: {message.guild} [{message.guild.id}] \n"
                      f"Channel: {message.channel.name} [{message.channel.id}] \n"
                      f"Timestamp: {timestamp}\n"
                      f"```**Content:**\n {message.content}")
            
            if message.attachments:
                for i, attachment in enumerate(message.attachments):
                    content += f"\n**Attachment {i+1}:** {attachment.url}"
                    
            async with session.post(WEBHOOK_URL, json={"content": content}) as response:
                if response.status != 204:
                    logger.warning(f"Webhook post failed with status {response.status}")
                    
    except Exception as e:
        logger.error(f"Error sending to webhook: {e}")

@client.event
async def on_ready():
    logger.info('WEBHOOK LOGGER')
    logger.info('github.com/Master0fFate')
    logger.info(f'Logged in as {client.user.name} ({client.user.id})')

@client.event
async def on_message(message):
    if message.guild is None:
        return
    
    if message.author.bot:
        return
    
    await send_to_webhook(message)

try:
    client.run(TOKEN)
except KeyboardInterrupt:
    logger.info("Shutting down gracefully...")
except Exception as e:
    logger.error(f"Error running client: {e}")
