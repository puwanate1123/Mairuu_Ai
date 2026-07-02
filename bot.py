import discord
from discord import app_commands
import openai
import os
import asyncio
import tempfile
from collections import deque
from datetime import datetime, date
from aiohttp import web

# ==================== โหลด Token จาก Environment Variables ====================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY') 

if not DISCORD_TOKEN:
    raise ValueError("❌ ไม่พบ DISCORD_TOKEN ใน Environment Variables")
if not GEMINI_API_KEY:
    raise ValueError("❌ ไม่พบ GEMINI_API_KEY ใน Environment Variables")
if not GROQ_API_KEY:
    raise ValueError("❌ ไม่พบ GROQ_API_KEY ใน Environment Variables")

# ==================== ตั้งค่า AI Clients (Gemini & Groq) ====================
client_gemini = openai.OpenAI(
    api_key=GEMINI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

client_groq = openai.OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

# ==================== ตั้งค่า Discord Intents ====================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)

# ==================== ค่าตั้งต้น ====================
COOLDOWN_SECONDS = int(os.getenv('COOLDOWN_SECONDS', '15'))       
MAX_DAILY_REQUESTS = int(os.getenv('MAX_DAILY_REQUESTS', '100'))  
MAX_PROMPT_LENGTH = int(os.getenv('MAX_PROMPT_LENGTH', '1000'))   
MIN_PROMPT_LENGTH = int(os.getenv('MIN_PROMPT_LENGTH', '3'))      
ALLOWED_CHANNEL_ID = int(os.getenv('ALLOWED_CHANNEL_ID', '1522145630062117016'))
ALLOWED_ROLE_ID = int(os.getenv('ALLOWED_ROLE_ID', '0'))

SYSTEM_PROMPT = """
คุณคือผู้เชี่ยวชาญด้านการสื่อสารที่มีประสิทธิภาพ 
หลักการตอบของคุณคือ:
1. **Precision First:** ตอบตรงประเด็นทันที ไม่ต้องมีคำเกริ่นนำที่ยาวเหยียด (เช่น ไม่ต้องพูดว่า "ได้เลยครับ ผมยินดีที่จะช่วย...") 
2. **Efficiency:** ใช้ภาษาที่กระชับและได้ใจความที่สุด ตัดคำฟุ่มเฟือยออก แต่ต้องรักษาเนื้อหาสำคัญไว้ครบถ้วน
3. **Structured:** หากข้อมูลมีหลายส่วน ให้ใช้ Bullet points หรือตาราง เพื่อความชัดเจนและประหยัดพื้นที่
4. **Constraint:** ห้ามสรุปคำตอบให้สั้นจนเสียเนื้อหาสำคัญ และห้ามตัดจบกลางคันเด็ดขาด
"""
CONVERSATION_HISTORY_LIMIT = 6

# ==================== ระบบจำกัดการใช้งาน ====================
cooldown_dict = {}

def check_cooldown(user_id):
    current_time = datetime.now().timestamp()
    if user_id in cooldown_dict:
        last_used = cooldown_dict[user_id]
        if current_time - last_used < COOLDOWN_SECONDS:
            return False, int(COOLDOWN_SECONDS - (current_time - last_used))
    cooldown_dict[user_id] = current_time
    return True, 0

_daily_usage = {"date": date.today(), "count": 0}

def check_daily_limit():
    today = date.today()
    if _daily_usage["date"] != today:
        _daily_usage["date"] = today
        _daily_usage["count"] = 0
    if _daily_usage["count"] >= MAX_DAILY_REQUESTS:
        return False
    _daily_usage["count"] += 1
    return True

def has_role_permission(author) -> bool:
    if ALLOWED_ROLE_ID == 0:
        return True
    if not isinstance(author, discord.Member):
        return False
    return any(role.id == ALLOWED_ROLE_ID for role in author.roles)

conversation_history = {}  

ERROR_MESSAGES = {
    "rate_limit": "🚦 ตอนนี้มีคนใช้เยอะ (โดน rate limit จาก AI ทั้งหมด) กรุณาลองใหม่อีกสักครู่ครับ",
    "temporary": "⚠️ เชื่อมต่อ AI ไม่สำเร็จชั่วคราว กรุณาลองใหม่อีกครั้งครับ",
}

def format_error(error_code):
    return ERROR_MESSAGES.get(error_code, f"❌ เกิดข้อผิดพลาด: {error_code}")

# ==================== ฟังก์ชันเรียก AI (เพิ่มขยาย Token) ====================
async def call_ai(user_id, prompt, image_url=None):
    history = conversation_history.setdefault(
        user_id, deque(maxlen=CONVERSATION_HISTORY_LIMIT)
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)

    if image_url:
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    else:
        user_content = prompt
    messages.append({"role": "user", "content": user_content})

    groq_model = "llama-3.2-11b-vision-preview" if image_url else "llama-3.3-70b-versatile"

    providers = [
        {"client": client_gemini, "model": "gemini-2.5-flash", "name": "Gemini"},
        {"client": client_groq, "model": groq_model, "name": "Groq"}
    ]

    last_error = "temporary"
    
    for p in providers:
        try:
            response = await asyncio.to_thread(
                p["client"].chat.completions.create,
                model=p["model"],
                messages=messages,
                max_tokens=4000,  # 🔥 [แก้ไข] ขยายขีดจำกัดจาก 1500 เป็น 4000 รองรับภาษาไทยยาวๆ ไม่ให้ขาดตอน
                temperature=0.7
            )
            answer = response.choices[0].message.content
            
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": answer})
            return answer, None
            
        except openai.RateLimitError:
            print(f"🚦 {p['name']} ติด Rate Limit / โควตาเต็ม กำลังสลับไปตัวถัดไป...")
            last_error = "rate_limit"
            continue
        except Exception as e:
            print(f"⚠️ {p['name']} เกิดข้อผิดพลาด: {e} | กำลังสลับไปตัวถัดไป...")
            last_error = "temporary"
            continue

    return None, last_error

# ==================== ฟังก์ชันช่วยส่งคำตอบแบบ Embed ====================
async def send_ai_response(send_func, answer, question=None):
    embed = discord.Embed(color=discord.Color.blurple())
    if question:
        embed.add_field(name="❓ คำถาม", value=question[:1024], inline=False)

    if len(answer) > 4000:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(answer)
                tmp_path = f.name
            embed.description = "🤖 **คำตอบ:**\nคำตอบยาวเกินไป ส่งเป็นไฟล์แนบให้ครับ 👇"
            await send_func(embed=embed, file=discord.File(tmp_path, filename="answer.txt"))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    else:
        embed.description = f"🤖 **คำตอบ:**\n\n{answer}"
        await send_func(embed=embed)

# ==================== Event: Bot พร้อมใช้งาน ====================
@bot.event
async def on_ready():
    print(f'✅ บอท {bot.user} พร้อมใช้งานแล้ว!')
    print(f'📊 กำลังเชื่อมต่อกับ {len(bot.guilds)} เซิร์ฟเวอร์')
    await tree.sync()

# ==================== Slash Commands ====================
@tree.command(name="help", description="วิธีใช้งานบอท AI")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 วิธีใช้งานบอท", color=discord.Color.green())
    embed.add_field(
        name="💬 พิมพ์ถามตรงๆ",
        value=f"พิมพ์คำถามได้เลยใน <#{ALLOWED_CHANNEL_ID}> ไม่ต้องมีคำสั่งนำหน้า บอทตอบทันที",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reset", description="ล้างบทสนทนาที่คุยกับ AI ไว้ (เริ่มคุยใหม่)")
async def reset_cmd(interaction: discord.Interaction):
    conversation_history.pop(interaction.user.id, None)
    await interaction.response.send_message("🔄 ล้างบทสนทนาก่อนหน้าแล้วครับ เริ่มคุยใหม่ได้เลย", ephemeral=True)

@tree.command(name="config", description="[Admin] ปรับค่าลิมิตของบอทแบบ real-time")
@app_commands.describe(setting="ตัวเลือกที่จะปรับ", value="ค่าใหม่ (ตัวเลข)")
@app_commands.choices(setting=[
    app_commands.Choice(name="Cooldown (วินาที/คน)", value="cooldown"),
    app_commands.Choice(name="โควตารวมต่อวัน (ครั้ง)", value="daily"),
    app_commands.Choice(name="ความยาวคำถามสูงสุด (ตัวอักษร)", value="prompt_max"),
    app_commands.Choice(name="ความยาวคำถามต่ำสุดที่จะตอบ (ตัวอักษร)", value="prompt_min"),
])
async def config_cmd(interaction: discord.Interaction, setting: app_commands.Choice[str], value: int):
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 คำสั่งนี้ใช้ได้เฉพาะแอดมินเซิร์ฟเวอร์เท่านั้นครับ", ephemeral=True)
        return

    global COOLDOWN_SECONDS, MAX_DAILY_REQUESTS, MAX_PROMPT_LENGTH, MIN_PROMPT_LENGTH
    if value < 0:
        await interaction.response.send_message("⚠️ ค่าต้องเป็นตัวเลขที่ไม่ติดลบครับ", ephemeral=True)
        return

    if setting.value == "cooldown": COOLDOWN_SECONDS = value
    elif setting.value == "daily": MAX_DAILY_REQUESTS = value
    elif setting.value == "prompt_max": MAX_PROMPT_LENGTH = value
    elif setting.value == "prompt_min": MIN_PROMPT_LENGTH = value

    await interaction.response.send_message(f"✅ ตั้งค่า **{setting.name}** เป็น `{value}` แล้ว", ephemeral=True)

@tree.command(name="ask", description="ถาม Gemini AI (แนบรูปได้)")
@app_commands.describe(คำถาม="คำถามของคุณ", รูปภาพ="แนบรูปเพื่อถามเกี่ยวกับรูป (ไม่บังคับ)")
async def ask(interaction: discord.Interaction, คำถาม: str, รูปภาพ: discord.Attachment = None):
    if interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(f"🚫 คำสั่งนี้ใช้ได้เฉพาะใน <#{ALLOWED_CHANNEL_ID}> เท่านั้นครับ", ephemeral=True)
        return
    if not has_role_permission(interaction.user):
        await interaction.response.send_message(f"🚫 คุณไม่มีสิทธิ์ใช้คำสั่งนี้", ephemeral=True)
        return

    can_use, wait_time = check_cooldown(interaction.user.id)
    if not can_use:
        await interaction.response.send_message(f"⏳ โปรดรอ {wait_time} วินาทีก่อนใช้คำสั่งนี้อีกครั้ง", ephemeral=True)
        return
    if not check_daily_limit():
        await interaction.response.send_message("🚫 วันนี้มีคนใช้บอทครบโควตาแล้ว กรุณาลองใหม่พรุ่งนี้ครับ", ephemeral=True)
        return
    if len(คำถาม) > MAX_PROMPT_LENGTH:
        await interaction.response.send_message(f"⚠️ ข้อความยาวเกินไป", ephemeral=True)
        return

    image_url = รูปภาพ.url if รูปภาพ and รูปภาพ.content_type and รูปภาพ.content_type.startswith("image/") else None
    await interaction.response.defer()

    answer, error = await call_ai(interaction.user.id, คำถาม, image_url)
    if error:
        await interaction.followup.send(format_error(error))
        return

    await send_ai_response(interaction.followup.send, answer, question=คำถาม)

# ==================== Event: รับข้อความในแชท ====================
@bot.event
async def on_message(message):
    if message.author == bot.user or message.author.bot: return
    if message.channel.id != ALLOWED_CHANNEL_ID: return
    if not has_role_permission(message.author): return

    prompt = message.content.strip()
    image_url = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_url = att.url
            break

    if not prompt and not image_url: return
    if not prompt and image_url: prompt = "อธิบายรูปนี้ให้หน่อยครับ"
    if image_url is None and len(prompt) < MIN_PROMPT_LENGTH: return

    can_use, wait_time = check_cooldown(message.author.id)
    if not can_use:
        await message.channel.send(f"⏳ โปรดรอ {wait_time} วินาที")
        return
    if not check_daily_limit():
        await message.channel.send("🚫 วันนี้มีคนใช้บอทครบโควตาแล้ว")
        return
    if len(prompt) > MAX_PROMPT_LENGTH:
        await message.channel.send(f"⚠️ ข้อความยาวเกินไป")
        return

    async with message.channel.typing():
        answer, error = await call_ai(message.author.id, prompt, image_url)
        if error:
            await message.channel.send(format_error(error))
            return
        await send_ai_response(message.channel.send, answer)

# ==================== Web Server ====================
async def handle_ping(request): return web.Response(text="✅ Discord bot is running")
async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()

async def main():
    await start_webserver()
    async with bot: await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e: print(f"❌ ไม่สามารถรันบอทได้: {e}")
