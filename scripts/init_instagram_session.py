"""Helper CLI script to log into Instagram and generate a session ID.

Run this script to authenticate your Instagram burner account:
    python scripts/init_instagram_session.py
"""

import sys
from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    TwoFactorRequired,
    ChallengeRequired,
    RateLimitError,
)


def main():
    print("=" * 60)
    print("  Operatives Instagram Burner Account Session Generator")
    print("=" * 60)

    username = input("\nInstagram Username: ").strip()
    password = input("Instagram Password: ").strip()
    proxy = input("Proxy URL (optional, press Enter to skip): ").strip() or None

    cl = Client()
    if proxy:
        cl.set_proxy(proxy)

    print("\nAttempting login to Instagram...")
    try:
        logged_in = cl.login(username, password)
    except TwoFactorRequired:
        code = input("Enter 2FA verification code from your SMS / Authenticator app: ").strip()
        logged_in = cl.login(username, password, verification_code=code)
    except BadPassword:
        print("\n❌ Error: Incorrect username or password.")
        sys.exit(1)
    except ChallengeRequired:
        print("\n⚠️ Instagram requested a security challenge (SMS or email verification).")
        try:
            cl.challenge_resolve(cl.last_json)
            logged_in = True
        except Exception as e:
            print(f"❌ Challenge could not be solved automatically: {e}")
            sys.exit(1)
    except RateLimitError:
        print("\n❌ Error: Instagram rate-limited this IP. Try with a proxy or wait a while.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Login error: {e}")
        sys.exit(1)

    if logged_in:
        cookies = cl.cookie_dict
        sessionid = cookies.get("sessionid")

        print("\n" + "=" * 60)
        print("SUCCESSFULLY AUTHENTICATED WITH INSTAGRAM!")
        print(f"Logged in as: @{username}")
        print("=" * 60)

        if sessionid:
            print(f"\nYour Instagram sessionid is:\n{sessionid}\n")
            print("=" * 60)
            print("To add this session to your bot, send this message in your Telegram bot:")
            proxy_arg = f" {proxy}" if proxy else ""
            print(f"/add_session instagram {username} {sessionid}{proxy_arg}")
            print("=" * 60)
        else:
            print("⚠️ Warning: sessionid cookie not found in response dict.")


if __name__ == "__main__":
    main()
