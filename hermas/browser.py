"""Opt-in Playwright read-only preview. NOT a hardened network sandbox.

Uses allowlisted public DNS names and blocks all non-HTTP(S) subrequests.
Runs without credentials, file downloads, or persistent browser state.
Use a dedicated container/network namespace for untrusted websites.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


class BrowserError(ValueError):
    pass


def validate_url(url: str, allow_hosts: tuple[str, ...]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise BrowserError('URL harus HTTP(S) tanpa credentials atau fragment')
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserError('Port URL tidak valid') from exc
    if port and port not in (80, 443):
        raise BrowserError('Port browser hanya 80 atau 443')
    if parsed.hostname.lower() not in allow_hosts:
        raise BrowserError('Hostname tidak berada dalam allowlist eksplisit')
    if len(url) > 2048:
        raise BrowserError('URL terlalu panjang')
    try:
        records = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BrowserError('DNS tidak dapat ditemukan') from exc
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise BrowserError('Alamat privat, loopback atau non-publik diblokir')


async def preview(url: str, allow_hosts: tuple[str, ...]) -> dict:
    validate_url(url, allow_hosts)
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserError('Install paket opsional: pip install playwright && playwright install chromium') from exc
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(accept_downloads=False, service_workers='block', java_script_enabled=False)
            async def route_guard(route):
                target = route.request.url
                try:
                    validate_url(target, allow_hosts)
                    await route.continue_()
                except BrowserError:
                    await route.abort()
            await context.route('**/*', route_guard)
            page = await context.new_page()
            try:
                response = await asyncio.wait_for(page.goto(url, wait_until='domcontentloaded', timeout=12000), 15)
                if page.url != url:
                    validate_url(page.url, allow_hosts)
                heading = await page.title()
                body = await page.locator('body').inner_text(timeout=3000)
                return {'url':page.url,'status':response.status if response else None,'title':heading[:250], 'text':body[:10000]}
            finally:
                await context.close()
        finally:
            await browser.close()
