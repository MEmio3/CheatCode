import sys

def resolve():
    with open('cinebot/live/group_booking.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    parts = content.split('<<<<<<< HEAD\n')
    out = [parts[0]]
    for part in parts[1:]:
        head_part, rest = part.split('=======\n', 1)
        fca_part, remainder = rest.split('>>>>>>> 7d35631 (feat: Purchasing porjonto gese)\n', 1)
        
        # Conflict 1: _park_event vs _booking_lock
        if '_park_event: Optional' in head_part:
            res = head_part + fca_part
        # Conflict 2: _park_event = asyncio.Event() vs _booking_lock = asyncio.Lock()
        elif '_park_event = asyncio.Event()' in head_part:
            res = head_part + fca_part
        # Conflict 3: finally: self._task = None
        elif 'Browser windows stay open' in fca_part and head_part.strip() == '':
            res = fca_part
        # Conflict 4: We never close contexts implicitly
        elif 'We never close contexts implicitly' in head_part:
            res = head_part + fca_part
        elif 'stagger = max(0, session.index - 1)' in head_part:
            res = head_part
        # Conflict 5: Booking logic (keep fca94f1)
        elif 'self._set_session(session, "booking"' in head_part and 'async with self._booking_lock' in fca_part:
            res = fca_part
        # Conflict 6: OTP input locator vs stop automation (keep HEAD)
        elif 'otp_input = page.locator' in head_part:
            res = head_part
        # Conflict 7: finally block for _otp_future (keep HEAD)
        elif 'if session._otp_future is not None' in head_part:
            res = head_part
        # Conflict 8: _wait_for_payment_result timeout logic (Weave)
        elif 'failure_keywords = (' in head_part:
            fca_lines = fca_part.strip().split('\n')
            if 'raise GroupPlanError' in fca_lines[-1]:
                fca_lines = fca_lines[:-1]
            res = '\n'.join(fca_lines) + '\n        # On timeout, do NOT raise — leave the session in manual_otp so the\n        # user can still finish the OTP + PIN entry by hand.\n        return\n'
        # Conflict 9: _wait_for_seat candidates vs selectors (Keep fca94f1)
        elif 'candidates = (' in head_part:
            res = fca_part
        # Conflict 10: locator timeout (Keep fca94f1)
        elif 'timeout=timeout' in head_part:
            res = fca_part
        elif 'booking_payload = await' in head_part:
            res = fca_part
        else:
            raise Exception('Unknown conflict: \nHEAD:\n' + head_part[:100])
            
        out.append(res)
        out.append(remainder)

    with open('cinebot/live/group_booking.py', 'w', encoding='utf-8') as f:
        f.write(''.join(out))

resolve()
