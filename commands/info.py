"""Server Information commands cog for Xolby Discord Bot.

Provides commands:
/serverinfo, /userinfo, /roleinfo, /channelinfo, /avatar, /roles, /channels, /membercount
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class InfoCog(commands.Cog):
    """Server and user information commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ---------------------------------------------------------------------------
    # /serverinfo
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="serverinfo", description="Display information about the current server."
    )
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            return

        created_ts = int(guild.created_at.timestamp())
        owner = guild.owner or f"ID: {guild.owner_id}"

        embed = discord.Embed(
            title=f"📊 {guild.name}",
            color=0x3498DB,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Owner", value=str(owner), inline=True)
        embed.add_field(name="Created On", value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)", inline=False)
        embed.add_field(name="Members", value=f"👥 Total: **{guild.member_count:,}**", inline=True)
        embed.add_field(name="Channels", value=f"📁 Text/Voice: **{len(guild.channels)}**", inline=True)
        embed.add_field(name="Roles", value=f"🎭 Roles: **{len(guild.roles)}**", inline=True)
        embed.add_field(name="Verification", value=str(guild.verification_level).title(), inline=True)

        if guild.banner:
            embed.set_image(url=guild.banner.url)

        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /userinfo
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="userinfo", description="Display information about a user."
    )
    @app_commands.describe(user="The user to inspect.")
    async def userinfo(
        self, interaction: discord.Interaction, user: Optional[discord.User] = None
    ) -> None:
        target_user = user or interaction.user
        guild = interaction.guild

        member: Optional[discord.Member] = None
        if guild and isinstance(target_user, discord.User):
            member = guild.get_member(target_user.id)
        elif isinstance(target_user, discord.Member):
            member = target_user

        embed = discord.Embed(
            title=f"👤 {target_user.name}",
            color=member.top_role.color if member and member.top_role else 0x3498DB,
        )
        avatar_url = target_user.display_avatar.url
        embed.set_thumbnail(url=avatar_url)

        embed.add_field(name="Username", value=f"`{target_user.name}`", inline=True)
        embed.add_field(name="User ID", value=f"`{target_user.id}`", inline=True)
        embed.add_field(name="Is Bot", value="Yes" if target_user.bot else "No", inline=True)

        created_ts = int(target_user.created_at.timestamp())
        embed.add_field(name="Account Created", value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)", inline=False)

        if member:
            if member.joined_at:
                joined_ts = int(member.joined_at.timestamp())
                embed.add_field(name="Joined Server", value=f"<t:{joined_ts}:D> (<t:{joined_ts}:R>)", inline=False)

            roles = [r.mention for r in reversed(member.roles) if r != guild.default_role]
            if roles:
                role_str = " ".join(roles[:10])
                if len(roles) > 10:
                    role_str += f" (+{len(roles)-10} more)"
                embed.add_field(name=f"Roles ({len(roles)})", value=role_str, inline=False)

        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /roleinfo
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="roleinfo", description="Display information about a role."
    )
    @app_commands.describe(role="The role to inspect.")
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role) -> None:
        created_ts = int(role.created_at.timestamp())

        embed = discord.Embed(
            title=f"🎭 Role: {role.name}",
            color=role.color if role.color.value != 0 else 0x3498DB,
        )
        embed.add_field(name="Role ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Color", value=f"`{role.color}`", inline=True)
        embed.add_field(name="Position", value=str(role.position), inline=True)
        embed.add_field(name="Members", value=f"👥 **{len(role.members):,}**", inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
        embed.add_field(name="Created On", value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)", inline=False)

        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /channelinfo
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="channelinfo", description="Display information about the current channel."
    )
    async def channelinfo(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not channel:
            await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
            return

        created_ts = int(channel.created_at.timestamp())

        embed = discord.Embed(
            title=f"📁 #{getattr(channel, 'name', 'Channel')}",
            color=0x3498DB,
        )
        embed.add_field(name="Channel ID", value=f"`{channel.id}`", inline=True)
        embed.add_field(name="Type", value=str(channel.type).title(), inline=True)

        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)

        if hasattr(channel, "slowmode_delay"):
            embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s", inline=True)

        if hasattr(channel, "topic") and channel.topic:
            embed.add_field(name="Topic", value=channel.topic[:200], inline=False)

        embed.add_field(name="Created On", value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)", inline=False)

        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /avatar
    # ---------------------------------------------------------------------------
    @app_commands.command(name="avatar", description="Display a user's avatar.")
    @app_commands.describe(user="The user whose avatar to view.")
    async def avatar(
        self, interaction: discord.Interaction, user: Optional[discord.User] = None
    ) -> None:
        target = user or interaction.user
        avatar_url = target.display_avatar.url

        embed = discord.Embed(
            title=f"🖼️ Avatar of {target.name}",
            color=0x3498DB,
        )
        embed.set_image(url=avatar_url)

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="View Full Size", url=avatar_url, style=discord.ButtonStyle.link
            )
        )

        await interaction.response.send_message(embed=embed, view=view)

    # ---------------------------------------------------------------------------
    # /roles
    # ---------------------------------------------------------------------------
    @app_commands.command(name="roles", description="List all server roles.")
    async def roles(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            return

        all_roles = [r for r in reversed(guild.roles) if r != guild.default_role]
        if not all_roles:
            await interaction.response.send_message("No custom roles found in this server.")
            return

        embed = discord.Embed(
            title=f"🎭 Server Roles ({len(all_roles)})",
            color=0x3498DB,
        )

        lines = [f"• {r.mention} (`{len(r.members)}` members)" for r in all_roles[:25]]
        embed.description = "\n".join(lines)

        if len(all_roles) > 25:
            embed.set_footer(text=f"Showing top 25 of {len(all_roles)} roles.")

        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /channels
    # ---------------------------------------------------------------------------
    @app_commands.command(name="channels", description="List all channels in the server.")
    async def channels(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📁 Channels in {guild.name}",
            color=0x3498DB,
        )

        categories: dict[str, list[str]] = {}
        no_category = []

        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            name = f"#{ch.name}" if isinstance(ch, discord.TextChannel) else ch.name
            if ch.category:
                categories.setdefault(ch.category.name, []).append(name)
            else:
                no_category.append(name)

        desc_parts = []
        if no_category:
            desc_parts.append("**No Category**\n" + ", ".join(no_category[:15]))

        for cat_name, ch_list in list(categories.items())[:5]:
            desc_parts.append(f"📁 **{cat_name}**\n" + ", ".join(ch_list[:15]))

        embed.description = "\n\n".join(desc_parts)[:3800]
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /membercount
    # ---------------------------------------------------------------------------
    @app_commands.command(name="membercount", description="Show the total member count.")
    async def membercount(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            return

        total = guild.member_count or len(guild.members)
        bots = sum(1 for m in guild.members if m.bot)
        humans = total - bots

        embed = discord.Embed(
            title=f"👥 Member Count — {guild.name}",
            color=0x3498DB,
        )
        embed.add_field(name="Total Members", value=f"**{total:,}**", inline=False)
        embed.add_field(name="Humans", value=f"👤 **{humans:,}**", inline=True)
        embed.add_field(name="Bots", value=f"🤖 **{bots:,}**", inline=True)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InfoCog(bot))
    log.info("Loaded Info commands cog")
