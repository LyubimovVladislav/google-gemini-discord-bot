import asyncio
import io
import configparser

from PIL import Image
import aiohttp
import discord
from discord import app_commands, Message
from discord.ext import commands

import google.generativeai as genai
from google.generativeai.types import StopCandidateException, BlockedPromptException, safety_types


async def split_into_chunks(text):
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
    return chunks


def strip_message(message):
    message = message.strip()
    stripped_message = message.replace('<@1053053778963738745>', '').strip()
    return stripped_message


async def get_images(message):
    async with aiohttp.ClientSession() as session:
        result = []
        for attachment in message.attachments:
            if not attachment.filename.endswith(supported_formats):
                continue
            image_data = await attachment.read()
            result.append(Image.open(io.BytesIO(image_data)))
            # filedata: bytes = await self.download_attachment(attachment, session)
            # result.append(Image.open(io.BytesIO(filedata)))
    return result


def prevent_discord_mention_everyone(message):
    message = message.replace('@everyone', '*everyone')
    return message


# TODO: vision chat history integrated into the regular chat history


class Bot(commands.Bot):
    def __init__(self, /, command_prefix, intents, guild_id, safety_settings):
        super().__init__(command_prefix, intents=intents)
        self.guild = discord.Object(id=guild_id)
        self.model = genai.GenerativeModel('gemini-pro', safety_settings=safety_settings)
        self.vision = genai.GenerativeModel('gemini-pro-vision')
        self.chat = self.model.start_chat()
        self.register_app_commands()

    def register_app_commands(self):
        @self.tree.command(name='clear-chat-history', description='Clears chat history', guild=self.guild)
        async def clear_chat_history(interaction: discord.Message.interaction):
            self.chat.history.clear()
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.listening, name='0 messages in the chat history'))
            await interaction.response.send_message('Done.')

        @self.tree.command(name='message', description="It doesn't keep the previous chat messages", guild=self.guild)
        @app_commands.describe(message='Your message')
        async def generate_no_history(interaction: discord.Message.interaction, message: str):
            await interaction.response.defer(thinking=True)
            response = await self.model.generate_content_async(message)
            response = prevent_discord_mention_everyone(response.text)
            try:
                chunks = await split_into_chunks(response)
                for chunk in chunks:
                    await interaction.followup.send(chunk)
            except ValueError as e:
                await interaction.followup.send(ERR_MESSAGE)

    async def on_message(self, message: Message, /) -> None:
        if message.author.bot or message.author == self.user:
            return

        stripped_message = strip_message(message.content)
        if self.user not in message.mentions:
            return

        if not stripped_message and not message.attachments:
            await message.channel.send('Your message is empty', reference=message)
            return

        if message.attachments:
            await self.handle_attachment(message, stripped_message)
            return

        if message.reference is None:
            await self.process_message(message, stripped_message)
        elif message.reference and (self.user in message.mentions):
            referenced_message = (await message.channel.fetch_message(
                message.reference.message_id)).content if message.reference.cached_message is None else message.reference.cached_message.content
            await self.process_message(message, stripped_message, reference=referenced_message)

    async def handle_attachment(self, message, stripped_message):
        images = await get_images(message)
        if not images:
            await message.channel.send(f'No images in attachments. Supported formats: {", ".join(supported_formats)}',
                                       reference=message)
            return
        async with message.channel.typing():
            try:
                response = await self.vision.generate_content_async([stripped_message, *images])
            except (ValueError, StopCandidateException, BlockedPromptException) as e:
                await message.channel.send(ERR_MESSAGE, reference=message)
                return
            try:
                text = ''
                for part in response.parts:
                    text += part.text + '\n'
                text = prevent_discord_mention_everyone(text)
                chunks = await split_into_chunks(text)
                for chunk in chunks:
                    await message.channel.send(chunk, reference=message)
            except (ValueError, StopCandidateException, BlockedPromptException) as e:
                await message.channel.send(ERR_MESSAGE, reference=message)

    @staticmethod
    async def download_attachment(attachment: discord.Attachment, session: aiohttp.ClientSession) -> bytes:
        async with session.get(attachment.url) as resp:
            res = b''
            while not resp.content.at_eof():
                chunk = await resp.content.read(1024)
                res += chunk
        return res

    async def process_message(self, message, stripped_message, reference=None):
        async with semaphore:
            async with message.channel.typing():
                try:
                    if reference:
                        response = await self.chat.send_message_async(f'Reference: "{reference}"\n{stripped_message}')
                    else:
                        response = await self.chat.send_message_async(stripped_message)
                    response = prevent_discord_mention_everyone(response.text)
                    chunks = await split_into_chunks(response)
                    for chunk in chunks:
                        await message.channel.send(chunk, reference=message)
                except (ValueError, StopCandidateException, BlockedPromptException) as e:
                    await message.channel.send(ERR_MESSAGE, reference=message)
                finally:
                    await self.change_presence(
                        activity=discord.Activity(type=discord.ActivityType.listening,
                                                  name=f'{len(self.chat.history)} message(s) in the chat history'))

    async def on_ready(self):
        try:
            await self.tree.sync(guild=self.guild)
        except Exception as e:
            print(e)
        print(f'Logged in as {self.user}')
        print('Ready!')
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name='0 messages in the chat history'))


def read_config(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    config = config['DEFAULT']
    google_api_key = config.get('GoogleGeminiApiKey', '')
    guild_id = config.get('DiscordGuildId', '123')
    try:
        guild_id = int(guild_id)
    except ValueError:
        print('Guild id should be a number')
        exit()
    bot_api_key = config.get('DiscordBotApiKey', '')
    return google_api_key, guild_id, bot_api_key


if __name__ == '__main__':
    supported_formats = ('.jpg', '.jpeg', '.png', '.webp')
    GOOGLE_API_KEY, GUILD_ID, BOT_API_KEY = read_config('config.ini')
    semaphore = asyncio.BoundedSemaphore(1)
    ERR_MESSAGE = 'https://i.imgur.com/DJqE6wq.jpeg'
    safety = [{"category": safety_types.HarmCategory.HARM_CATEGORY_SEXUAL,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_MEDICAL,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_DANGEROUS,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_TOXICITY,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_VIOLENCE,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_DEROGATORY,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_UNSPECIFIED,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE,
               "category": safety_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
               "threshold": safety_types.HarmBlockThreshold.BLOCK_NONE}]
    genai.configure(api_key=GOOGLE_API_KEY)
    bot = Bot(
        intents=discord.Intents.all(),
        command_prefix='!',
        guild_id=GUILD_ID,
        safety_settings=safety
    )
    bot.run(BOT_API_KEY)
