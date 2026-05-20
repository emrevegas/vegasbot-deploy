
from modules.database import get_data, set_data
admins = get_data('server/admins') or {}
admins['890502170359779329'] = ['admin']
set_data('server/admins', admins)
print('Done')
