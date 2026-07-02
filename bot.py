import discord
import openai
import os
import asyncio
from datetime import datetime

# ==================== โหลด Token จาก Environment Variables ====================
# IMPORTANT: ห้ามใส่ Token ตรงนี้เด็ดขาด! 
# Render จะ inject ค่าให้อัตโนมัติจาก Environment Variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')

# ตรวจสอบว่ามีการตั้งค่าหรือไม่
if not DISCORD_TOKEN:
    raise ValueError("❌ ไม่พบ DISCORD_TOKEN ใน Environment Variables")
if not DEEPSEEK_API_KEY:
    raise ValueError("❌ ไม่พบ DEEPSEEK_API_KEY ใน Environment Variables")

# ==================== ตั้งค่า OpenAI Client ====================
client_openai = openai.OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"
)

# ==================== ตั้งค่า Discord Intents ====================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)

# ==================== ระบบจำกัดการใช้งาน (Cooldown) ====================
cooldown_dict = {}
COOLDOWN_SECONDS = 10  # รอ 10 วินาทีระหว่างแต่ละคำขอ

def check_cooldown(user_id):
    current_time = datetime.now().timestamp()
    if user_id in cooldown_dict:
        last_used = cooldown_dict[user_id]
        if current_time - last_used < COOLDOWN_SECONDS:
            return False, int(COOLDOWN_SECONDS - (current_time - last_used))
    cooldown_dict[user_id] = current_time
    return True, 0

# ==================== ฟังก์ชันเรียก DeepSeek ====================
async def call_deepseek(prompt):
    try:
        response = client_openai.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "คุณคือผู้ช่วยที่มีประโยชน์ ตอบอย่างสุภาพและเป็นมิตร ใช้ภาษาไทยให้เหมาะสม"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=500,  # ตั้งให้ประหยัด
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"❌ เกิดข้อผิดพลาด: {str(e)}"

# ==================== Event: Bot พร้อมใช้งาน ====================
@bot.event
async def on_ready():
    print(f'✅ บอท {bot.user} พร้อมใช้งานแล้ว!')
    print(f'📊 กำลังเชื่อมต่อกับ {len(bot.guilds)} เซิร์ฟเวอร์')
    
    # ซิงค์ Slash Command
    await tree.sync()
    print("✅ Slash Commands ซิงค์เรียบร้อย")
    
    # ตั้งสถานะให้ Bot
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"/ask | {len(bot.guilds)} Servers"
        )
    )

# ==================== Slash Command: /ask ====================
@tree.command(name="ask", description="ถาม DeepSeek AI")
async def ask(interaction: discord.Interaction, *, คำถาม: str):
    """คำสั่ง /ask <ข้อความ>"""
    
    # ตรวจสอบ Cooldown
    can_use, wait_time = check_cooldown(interaction.user.id)
    if not can_use:
        await interaction.response.send_message(
            f"⏳ โปรดรอ {wait_time} วินาทีก่อนใช้คำสั่งนี้อีกครั้ง", 
            ephemeral=True
        )
        return
    
    # แสดงสถานะกำลังคิด
    await interaction.response.defer()
    
    try:
        answer = await call_deepseek(คำถาม)
        
        # ตรวจสอบความยาว
        if len(answer) > 2000:
            # ส่งเป็นไฟล์
            with open("answer.txt", "w", encoding="utf-8") as f:
                f.write(answer)
            await interaction.followup.send(
                "📄 คำตอบยาวเกินไป ส่งเป็นไฟล์ให้ครับ:",
                file=discord.File("answer.txt")
            )
        else:
            await interaction.followup.send(answer)
            
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {str(e)}")

# ==================== Event: รับข้อความในแชท ====================
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    # ตรวจสอบ Cooldown
    can_use, wait_time = check_cooldown(message.author.id)
    if not can_use:
        await message.channel.send(f"⏳ โปรดรอ {wait_time} วินาที")
        return
    
    # ถ้ามีการ Mention หรือพิมพ์ !ai
    if bot.user in message.mentions or message.content.startswith("!ai"):
        # ดึงข้อความจริง
        if message.content.startswith("!ai"):
            prompt = message.content[4:].strip()
        else:
            for mention in message.mentions:
                prompt = message.content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "").strip()
        
        if not prompt:
            await message.channel.send("❓ กรุณาพิมพ์คำถามด้วยครับ เช่น `!ai สวัสดี`")
            return
        
        # แสดงสถานะกำลังพิมพ์
        async with message.channel.typing():
            answer = await call_deepseek(prompt)
            
            if len(answer) > 2000:
                with open("answer.txt", "w", encoding="utf-8") as f:
                    f.write(answer)
                await message.channel.send("📄 คำตอบยาวเกินไป ส่งเป็นไฟล์ให้ครับ:", file=discord.File("answer.txt"))
            else:
                await message.channel.send(answer)

# ==================== รัน Bot ====================
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"❌ ไม่สามารถรันบอทได้: {e}")