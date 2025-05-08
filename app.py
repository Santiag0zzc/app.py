from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from markupsafe import Markup
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import requests
import os
from datetime import datetime, timedelta
import json
from functools import wraps

app = Flask(__name__)
app.secret_key = 'clave_secreta_aplicacion_tickets_gnp'

@app.template_filter('nl2br')
def nl2br_filter(text):
    if text:
        return Markup(text.replace('\n', '<br>'))
    return text
@app.template_filter('date')
def date_filter(date, format='%d/%m/%Y %H:%M'):
    if date is None:
        return '-'
    if isinstance(date, str):
        try:
            date = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return date
    return date.strftime(format)

@app.template_test('contains')
def contains(value, other):
    """
    Prueba si una cadena contiene otra subcadena
    
    Args:
        value: La cadena donde buscar
        other: La subcadena a buscar
    
    Returns:
        bool: True si 'other' está contenido en 'value', False en caso contrario
    """
    if value is None:
        return False
    return other in value

@app.template_filter('count_asesor_messages')
def count_asesor_messages(mensajes, tipo='asesor'):
    """
    Counts messages by type in a list of messages
    
    Args:
        mensajes: List of message dictionaries
        tipo: The type of message to count ('asesor' or 'tecnico')
    
    Returns:
        int: Number of messages matching the specified type
    """
    if not mensajes:
        return 0
    
    count = 0
    for mensaje in mensajes:
        # Check if it's a message from an advisor (not from a technician)
        if tipo == 'asesor' and not mensaje['enviado_por'].startswith('Técnico:'):
            count += 1
        # Check if it's a message from a technician
        elif tipo == 'tecnico' and mensaje['enviado_por'].startswith('Técnico:'):
            count += 1
    
    return count

# Register the filter in the Jinja environment
app.jinja_env.filters['count_asesor_messages'] = count_asesor_messages

def get_estado_color(estado):
    """Retorna el color Bootstrap correspondiente al estado del ticket"""
    estados_colores = {
        'pendiente': 'danger',
        'en_proceso': 'warning',
        'en_espera': 'info',
        'resuelto': 'success',
        'cerrado': 'secondary'
    }
    return estados_colores.get(estado, 'secondary')

# Y luego añadir esta línea después de la definición de la función:
app.jinja_env.globals['get_estado_color'] = get_estado_color

@app.template_filter('get_estado_color')
def get_estado_color(estado):
    """Retorna el color Bootstrap correspondiente al estado del ticket"""
    estados_colores = {
        'pendiente': 'danger',
        'en_proceso': 'warning',
        'en_espera': 'info',
        'resuelto': 'success',
        'cerrado': 'secondary'
    }
    return estados_colores.get(estado, 'secondary')

# Configuración de MySQL
app.config['MYSQL_HOST'] = '127.0.0.1'
app.config['MYSQL_PORT'] = 3306
app.config['MYSQL_USER'] = 'root'
app.config['MYSQL_PASSWORD'] = ''
app.config['MYSQL_DB'] = 'botgnp'
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

# Inicializar extensiones
mysql = MySQL(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Configuración del bot de Telegram
TELEGRAM_TOKEN = '7723454775:AAFqsrQTHXUIuwYfmsbI2HcRn17ybe9whvY'
TELEGRAM_API_URL = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}'

# Modelo de Usuario para Flask-Login
class User(UserMixin):
    def __init__(self, id, username, nombre, role):
        self.id = id
        self.username = username
        self.nombre = nombre
        self.role = role

# Añade esto después de la definición de la app
@app.context_processor
def inject_now():
    return {'now': datetime.utcnow}

@login_manager.user_loader
def load_user(user_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM usuarios WHERE id = %s", (user_id,))
    user = cur.fetchone()
    cur.close()
    
    if not user:
        return None
    
    return User(
        id=user['id'],
        username=user['username'],
        nombre=user['nombre'],
        role=user['role']
    )

# Decorador para verificar roles
def role_required(roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                flash('No tienes permiso para acceder a esta página', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Rutas
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# Función para actualizar el estado_gestion según el estado
def actualizar_estado_gestion(ticket_id, nuevo_estado, usuario_id=None, usuario_nombre=None):
    cur = mysql.connection.cursor()
    
    # Obtener el ticket actual
    cur.execute("SELECT estado, estado_gestion FROM registros_ot WHERE id = %s", (ticket_id,))
    ticket = cur.fetchone()
    
    if not ticket:
        cur.close()
        return False
    
    # Si el ticket es nuevo (no tiene estado_gestion) o cambia el estado, actualizamos estado_gestion
    if not ticket['estado_gestion'] or ticket['estado'] != nuevo_estado:
        # Actualizar estado_gestion para que refleje el estado
        estado_gestion_map = {
            'pendiente': 'Nuevo',
            'en_proceso': 'En atención',
            'en_espera': 'En espera',
            'resuelto': 'Resuelto',
            'cerrado': 'Cerrado'
        }
        
        nuevo_estado_gestion = estado_gestion_map.get(nuevo_estado, 'Nuevo')
        
        # Actualizar en la base de datos
        cur.execute("""
            UPDATE registros_ot 
            SET estado_gestion = %s,
                fecha_actualizacion = NOW()
            WHERE id = %s
        """, (nuevo_estado_gestion, ticket_id))
        
        mysql.connection.commit()
        
        # Registrar en historial si tenemos información del usuario
        if usuario_id and usuario_nombre:
            cur.execute("""
                INSERT INTO historial_tickets (ticket_id, usuario_id, nombre_usuario, tipo_cambio, descripcion, fecha)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (ticket_id, usuario_id, usuario_nombre, 'cambio_estado_gestion', 
                 f'Estado de gestión actualizado automáticamente a: {nuevo_estado_gestion} basado en el estado: {nuevo_estado}'))
            mysql.connection.commit()
        
        cur.close()
        return True
    
    cur.close()
    return False

# Ruta API para obtener estadísticas del dashboard
@app.route('/api/dashboard/stats')
@login_required
def api_dashboard_stats():
    cur = mysql.connection.cursor()
    
    # Contar tickets por estado
    cur.execute("""
        SELECT estado, COUNT(*) as total FROM registros_ot 
        GROUP BY estado
    """)
    stats_estado = cur.fetchall()
    
    cur.close()
    
    # Convertir a formato más fácil de usar en JavaScript
    estados = {}
    for estado in stats_estado:
        estados[estado['estado']] = estado['total']
    
    return jsonify({
        'estados': estados
    })

# Ruta API para obtener tickets del dashboard
@app.route('/api/dashboard/tickets')
@login_required
def api_dashboard_tickets():
    cur = mysql.connection.cursor()
    
    # Obtener tickets recientes
    cur.execute("""
        SELECT id, ticket, nombre_cliente, telefono, estado, estado_gestion, 
               DATE_FORMAT(fecha_creacion, '%Y-%m-%d %H:%i:%s') as fecha_creacion
        FROM registros_ot 
        ORDER BY fecha_creacion DESC 
        LIMIT 10
    """)
    tickets_recientes = cur.fetchall()
    
    # Obtener tickets asignados al usuario actual
    cur.execute("""
        SELECT id, ticket, nombre_cliente, telefono, estado, estado_gestion,
               DATE_FORMAT(fecha_creacion, '%Y-%m-%d %H:%i:%s') as fecha_creacion
        FROM registros_ot 
        WHERE bloqueado_por = %s AND estado != 'cerrado'
        ORDER BY fecha_creacion DESC
    """, (current_user.id,))
    mis_tickets = cur.fetchall()
    
    cur.close()
    
    return jsonify({
        'tickets_recientes': tickets_recientes,
        'mis_tickets': mis_tickets
    })

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        
        if user and bcrypt.check_password_hash(user['password'], password):
            user_obj = User(
                id=user['id'],
                username=user['username'],
                nombre=user['nombre'],
                role=user['role']
            )
            login_user(user_obj)
            flash('Has iniciado sesión correctamente', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Usuario o contraseña incorrectos', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Has cerrado sesión correctamente', 'success')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    # Obtener tickets
    cur = mysql.connection.cursor()
    
    # Contar tickets por estado
    cur.execute("""
        SELECT estado, COUNT(*) as total FROM registros_ot 
        GROUP BY estado
    """)
    stats_estado = cur.fetchall()
    
    # Obtener tickets recientes
    cur.execute("""
        SELECT * FROM registros_ot 
        ORDER BY fecha_creacion DESC 
        LIMIT 10
    """)
    tickets_recientes = cur.fetchall()
    
    # Obtener tickets asignados al usuario actual
    cur.execute("""
        SELECT * FROM registros_ot 
        WHERE bloqueado_por = %s AND estado != 'cerrado'
        ORDER BY fecha_creacion DESC
    """, (current_user.id,))
    mis_tickets = cur.fetchall()
    
    cur.close()
    
    return render_template('dashboard.html', 
                           stats_estado=stats_estado,
                           tickets_recientes=tickets_recientes,
                           mis_tickets=mis_tickets)

# El resto del código permanece igual...

@app.route('/tickets')
@login_required
def tickets():
    estado_filtro = request.args.get('estado', '')
    estado_gestion_filtro = request.args.get('estado_gestion', '')
    busqueda = request.args.get('busqueda', '')
    
    cur = mysql.connection.cursor()
    
    query_base = """
        SELECT r.*, u.nombre as nombre_asesor 
        FROM registros_ot r
        LEFT JOIN usuarios u ON r.bloqueado_por = u.id
        WHERE 1=1
    """
    params = []
    
    if estado_filtro:
        query_base += " AND r.estado = %s"
        params.append(estado_filtro)
    
    if estado_gestion_filtro:
        query_base += " AND r.estado_gestion = %s"
        params.append(estado_gestion_filtro)
    
    if busqueda:
        # Fix: Adding just one placeholder and one parameter
        query_base += " AND r.ticket LIKE %s"
        params.append(f'%{busqueda}%')
    
    query_base += " ORDER BY r.fecha_creacion DESC"
    
    cur.execute(query_base, params)
    tickets = cur.fetchall()
    
    # Obtener lista de posibles estados de gestión para el filtro
    cur.execute("SELECT DISTINCT estado_gestion FROM registros_ot WHERE estado_gestion IS NOT NULL")
    estados_gestion = [row['estado_gestion'] for row in cur.fetchall()]
    
    cur.close()
    
    return render_template('tickets.html', 
                          tickets=tickets, 
                          estado_filtro=estado_filtro,
                          estado_gestion_filtro=estado_gestion_filtro,
                          estados_gestion=estados_gestion,
                          busqueda=busqueda)

# Modificar la función ver_ticket para mostrar correctamente los mensajes del técnico
@app.route('/ticket/<string:numero_ot>', methods=['GET', 'POST'])
@login_required
def ver_ticket(numero_ot):
    cur = mysql.connection.cursor()
    
    # Obtener detalles del ticket con todos los campos necesarios de registros_ot
    cur.execute("""
        SELECT r.*, u.nombre as nombre_asesor 
        FROM registros_ot r
        LEFT JOIN usuarios u ON r.bloqueado_por = u.id
        WHERE r.ticket = %s
    """, (numero_ot,))
    ticket = cur.fetchone()
    
    if not ticket:
        flash('Ticket no encontrado', 'danger')
        return redirect(url_for('tickets'))
    
    # CORRECCIÓN: Primero intentamos buscar mensajes usando el ID del ticket como chat_id
    cur.execute(""" 
        SELECT * FROM mensajes_tecnicos 
        WHERE chat_id = %s 
        ORDER BY fecha_envio ASC 
    """, (str(ticket['id']),))
    todos_mensajes = cur.fetchall()
    
    # Log para depuración
    app.logger.info(f"Ticket #{numero_ot}: Se encontraron {len(todos_mensajes)} mensajes usando ID del ticket")
    
    # Si no hay mensajes Y existe un chat_id en el ticket, buscar por ese valor
    if not todos_mensajes and ticket['chat_id']:
        app.logger.info(f"Buscando mensajes por chat_id del ticket: {ticket['chat_id']}")
        cur.execute(""" 
            SELECT * FROM mensajes_tecnicos 
            WHERE chat_id = %s 
            ORDER BY fecha_envio ASC 
        """, (ticket['chat_id'],))
        todos_mensajes = cur.fetchall()
        app.logger.info(f"Búsqueda por chat_id del ticket: Se encontraron {len(todos_mensajes)} mensajes")
    
    # Obtener historial de cambios del ticket
    cur.execute("""
        SELECT * FROM historial_tickets
        WHERE ticket_id = %s
        ORDER BY fecha DESC
    """, (ticket['id'],))
    historial = cur.fetchall()
    
    if request.method == 'POST':
        accion = request.form.get('accion')
        
        if accion == 'bloquear':
            # Verificar si ya está bloqueado por otro usuario
            if ticket['bloqueado_por'] and ticket['bloqueado_por'] != current_user.id:
                flash(f'Este ticket ya está siendo gestionado por {ticket["nombre_asesor"]}', 'danger')
            else:
                # Bloquear ticket para este asesor
                cur.execute("""
                    UPDATE registros_ot 
                    SET bloqueado_por = %s, fecha_bloqueo = NOW()
                    WHERE ticket = %s
                """, (current_user.id, numero_ot))
                mysql.connection.commit()
                
                # Registrar en historial
                registrar_historial(cur, ticket['id'], 'asignacion', 
                                   f'Ticket asignado a {current_user.nombre}')
                
                flash('Ticket asignado a ti correctamente', 'success')
                return redirect(url_for('ver_ticket', numero_ot=numero_ot))
                
        elif accion == 'desbloquear':
            # Verificar que esté bloqueado por este usuario
            if ticket['bloqueado_por'] != current_user.id:
                flash('No puedes desbloquear un ticket que no está asignado a ti', 'danger')
            else:
                # Desbloquear ticket
                cur.execute("""
                    UPDATE registros_ot 
                    SET bloqueado_por = NULL, fecha_bloqueo = NULL
                    WHERE ticket = %s
                """, (numero_ot,))
                mysql.connection.commit()
                
                # Registrar en historial
                registrar_historial(cur, ticket['id'], 'liberacion', 
                                   f'Ticket liberado por {current_user.nombre}')
                
                flash('Ticket liberado correctamente', 'success')
                return redirect(url_for('ver_ticket', numero_ot=numero_ot))
                
        elif accion == 'actualizar_estado':
            nuevo_estado = request.form.get('nuevo_estado')
            # Verificar que esté bloqueado por este usuario
            if ticket['bloqueado_por'] != current_user.id:
                flash('No puedes actualizar un ticket que no está asignado a ti', 'danger')
            else:
                estado_anterior = ticket['estado']
                # Actualizar estado
                cur.execute("""
                    UPDATE registros_ot 
                    SET estado = %s,
                        fecha_actualizacion = NOW()
                    WHERE ticket = %s
                """, (nuevo_estado, numero_ot))
                mysql.connection.commit()
                
                # Actualizar estado_gestion automáticamente según el estado
                actualizar_estado_gestion(ticket['id'], nuevo_estado, current_user.id, current_user.nombre)
                
                # Si se cierra el ticket, registrar fecha de finalización
                if nuevo_estado == 'cerrado' and estado_anterior != 'cerrado':
                    cur.execute("""
                        UPDATE registros_ot 
                        SET fecha_finalizacion = NOW()
                        WHERE ticket = %s
                    """, (numero_ot,))
                    mysql.connection.commit()
                
                # Registrar en historial
                registrar_historial(cur, ticket['id'], 'cambio_estado', 
                                   f'Estado actualizado de {estado_anterior} a {nuevo_estado} por {current_user.nombre}')
                
                # CORRECCIÓN: Determinar qué chat_id usar para notificaciones
                chat_id_to_use = ticket['chat_id'] if ticket['chat_id'] else str(ticket['id'])
                
                # Notificar al técnico
                if chat_id_to_use:
                    mensaje = f"📋 *Actualización de Ticket #{numero_ot}*\n\nEl estado ha sido actualizado a: *{nuevo_estado}*\n\nActualizado por: {current_user.nombre}"
                    send_telegram_message(chat_id_to_use, mensaje)
                    
                    # CORRECCIÓN: Guardar mensaje en la base de datos usando el chat_id correcto
                    cur.execute("""
                        INSERT INTO mensajes_tecnicos (chat_id, mensaje, enviado_por, fecha_envio)
                        VALUES (%s, %s, %s, NOW())
                    """, (chat_id_to_use, 
                         f"El estado ha sido actualizado a: {nuevo_estado}", current_user.nombre))
                    mysql.connection.commit()
                
                flash(f'Estado actualizado a: {nuevo_estado}', 'success')
                return redirect(url_for('ver_ticket', numero_ot=numero_ot))
        
        elif accion == 'actualizar_estado_gestion':
            nuevo_estado_gestion = request.form.get('nuevo_estado_gestion')
            comentario = request.form.get('comentario_estado_gestion', '')
            
            # Verificar que esté bloqueado por este usuario
            if ticket['bloqueado_por'] != current_user.id:
                flash('No puedes actualizar un ticket que no está asignado a ti', 'danger')
            else:
                estado_gestion_anterior = ticket['estado_gestion']
                # Actualizar estado de gestión
                cur.execute("""
                    UPDATE registros_ot 
                    SET estado_gestion = %s,
                        fecha_actualizacion = NOW()
                    WHERE id = %s
                """, (nuevo_estado_gestion, ticket['id']))
                mysql.connection.commit()
                
                # Registrar en historial
                registrar_historial(cur, ticket['id'], 'cambio_estado_gestion', 
                                   f'Estado de gestión actualizado de {estado_gestion_anterior or "No definido"} a {nuevo_estado_gestion} por {current_user.nombre}')
                
                # CORRECCIÓN: Determinar qué chat_id usar para notificaciones
                chat_id_to_use = ticket['chat_id'] if ticket['chat_id'] else str(ticket['id'])
                
                # Notificar al técnico
                if chat_id_to_use:
                    mensaje = f"📋 *Actualización de Ticket #{numero_ot}*\n\nEl estado de gestión ha sido actualizado a: *{nuevo_estado_gestion}*"
                    
                    if comentario:
                        mensaje += f"\n\nComentario: {comentario}"
                        
                    mensaje += f"\n\nActualizado por: {current_user.nombre}"
                    
                    send_telegram_message(chat_id_to_use, mensaje)
                    
                    # CORRECCIÓN: Guardar mensaje en la base de datos usando el chat_id correcto
                    cur.execute("""
                        INSERT INTO mensajes_tecnicos (chat_id, mensaje, enviado_por, fecha_envio)
                        VALUES (%s, %s, %s, NOW())
                    """, (chat_id_to_use, 
                         f"El estado de gestión ha sido actualizado a: {nuevo_estado_gestion}" + 
                         (f"\nComentario: {comentario}" if comentario else ""), 
                         current_user.nombre))
                    mysql.connection.commit()
                
                flash(f'Estado de gestión actualizado a: {nuevo_estado_gestion}', 'success')
                return redirect(url_for('ver_ticket', numero_ot=numero_ot))
                
        elif accion == 'enviar_mensaje':
            mensaje = request.form.get('mensaje')
            # Verificar que esté bloqueado por este usuario
            if ticket['bloqueado_por'] != current_user.id:
                flash('No puedes enviar mensajes desde un ticket que no está asignado a ti', 'danger')
            elif not mensaje:
                flash('El mensaje no puede estar vacío', 'warning')
            else:
                try:
                    # CORRECCIÓN: Determinar qué chat_id usar
                    chat_id_to_use = ticket['chat_id'] if ticket['chat_id'] else str(ticket['id'])
                    
                    # Guardar mensaje en base de datos
                    cur.execute("""
                        INSERT INTO mensajes_tecnicos (chat_id, mensaje, enviado_por, fecha_envio)
                        VALUES (%s, %s, %s, NOW())
                    """, (chat_id_to_use, mensaje, current_user.nombre))
                    mysql.connection.commit()
            
                    # Registrar en historial
                    registrar_historial(cur, ticket['id'], 'mensaje', 
                            f'Mensaje enviado por {current_user.nombre}')
            
                    # Enviar mensaje via Telegram
                    send_telegram_message(chat_id_to_use, 
                        f"📋 *Actualización de Ticket #{numero_ot}*\n\n{mensaje}\n\nEnviado por: {current_user.nombre}")
                    
                    # CORRECCIÓN: Obtener mensajes actualizados después de enviar el nuevo mensaje
                    cur.execute(""" 
                        SELECT * FROM mensajes_tecnicos 
                        WHERE chat_id = %s 
                        ORDER BY fecha_envio ASC 
                    """, (chat_id_to_use,))
                    todos_mensajes = cur.fetchall()
                    
                    flash('Mensaje enviado correctamente', 'success')
                    
                    # Renderizar la página con datos actualizados en lugar de redirigir
                    return render_template('detalle_ticket.html', 
                                          ticket=ticket, 
                                          todos_mensajes=todos_mensajes,
                                          historial=historial,
                                          mantener_chat_abierto=True)  # Añadir un flag para mantener el chat abierto
                    
                except Exception as e:
                   flash(f'Error al enviar mensaje: {str(e)}', 'danger')
                   app.logger.error(f"Error al enviar mensaje al técnico: {str(e)}")
    
    cur.close()
    return render_template('detalle_ticket.html', 
                          ticket=ticket, 
                          todos_mensajes=todos_mensajes,
                          historial=historial)
    
    cur.close()
    return render_template('detalle_ticket.html', 
                          ticket=ticket, 
                          todos_mensajes=todos_mensajes,
                          historial=historial)

# Función modificada para registrar cambios en el historial
def registrar_historial(cursor, ticket_id, tipo_cambio, descripcion):
    cursor.execute("""
        INSERT INTO historial_tickets (ticket_id, usuario_id, nombre_usuario, tipo_cambio, descripcion, fecha)
        VALUES (%s, %s, %s, %s, %s, NOW())
    """, (ticket_id, current_user.id, current_user.nombre, tipo_cambio, descripcion))
    mysql.connection.commit()

@app.route('/api/tickets/liberar_inactivos', methods=['POST'])
@login_required
@role_required(['admin'])
def liberar_tickets_inactivos():
    minutos = int(request.form.get('minutos', 30))
    
    cur = mysql.connection.cursor()
    
    # Obtener tickets que serán liberados para registro en historial
    cur.execute("""
        SELECT r.ticket, u.nombre
        FROM registros_ot r
        JOIN usuarios u ON r.bloqueado_por = u.id
        WHERE r.bloqueado_por IS NOT NULL
        AND r.fecha_bloqueo < DATE_SUB(NOW(), INTERVAL %s MINUTE)
    """, (minutos,))
    tickets_a_liberar = cur.fetchall()
    
    # Liberar tickets inactivos
    cur.execute("""
        UPDATE registros_ot
        SET bloqueado_por = NULL, fecha_bloqueo = NULL
        WHERE bloqueado_por IS NOT NULL
        AND fecha_bloqueo < DATE_SUB(NOW(), INTERVAL %s MINUTE)
    """, (minutos,))
    
    tickets_liberados = cur.rowcount
    mysql.connection.commit()
    
    # Registrar en historial
    for ticket in tickets_a_liberar:
        cur.execute("""
            INSERT INTO historial_tickets (ticket_id, usuario_id, nombre_usuario, tipo_cambio, descripcion, fecha)
            VALUES (%s, %s, %s, %s, %s, NOW())
        """, (ticket['ticket'], current_user.id, current_user.nombre, 'liberacion_automatica', 
             f'Ticket liberado automáticamente por inactividad (asignado previamente a {ticket["nombre"]})'))
    
    mysql.connection.commit()
    cur.close()
    
    return jsonify({'tickets_liberados': tickets_liberados})

def send_telegram_message(chat_id, message):
    data = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'Markdown'
    }
    try:
        response = requests.post(f'{TELEGRAM_API_URL}/sendMessage', data=data)
        return response.json()
    except Exception as e:
        app.logger.error(f"Error enviando mensaje por Telegram: {e}")
        return None

# Ruta para gestión de usuarios (solo admin)
@app.route('/usuarios', methods=['GET'])
@login_required
@role_required(['admin'])
def usuarios():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM usuarios ORDER BY nombre")
    usuarios_list = cur.fetchall()
    cur.close()
    
    return render_template('usuarios.html', usuarios=usuarios_list)

@app.route('/usuario/nuevo', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def nuevo_usuario():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        nombre = request.form['nombre']
        rol = request.form['rol']
        
        # Verificar si el usuario ya existe
        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
        if cur.fetchone():
            flash('El nombre de usuario ya está en uso', 'danger')
            return render_template('usuario_form.html')
        
        # Crear nuevo usuario
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        cur.execute("""
            INSERT INTO usuarios (username, password, nombre, role)
            VALUES (%s, %s, %s, %s)
        """, (username, hashed_password, nombre, rol))
        mysql.connection.commit()
        cur.close()
        
        flash('Usuario creado correctamente', 'success')
        return redirect(url_for('usuarios'))
    
    return render_template('usuario_form.html')

@app.route('/usuario/editar/<int:user_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def editar_usuario(user_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM usuarios WHERE id = %s", (user_id,))
    usuario = cur.fetchone()
    
    if not usuario:
        flash('Usuario no encontrado', 'danger')
        return redirect(url_for('usuarios'))
    
    if request.method == 'POST':
        nombre = request.form['nombre']
        rol = request.form['rol']
        
        # Actualizar usuario
        cur.execute("""
            UPDATE usuarios
            SET nombre = %s, role = %s
            WHERE id = %s
        """, (nombre, rol, user_id))
        
        # Si se proporcionó una nueva contraseña, actualizarla
        if request.form['password'].strip():
            hashed_password = bcrypt.generate_password_hash(request.form['password']).decode('utf-8')
            cur.execute("UPDATE usuarios SET password = %s WHERE id = %s", (hashed_password, user_id))
        
        mysql.connection.commit()
        cur.close()
        
        flash('Usuario actualizado correctamente', 'success')
        return redirect(url_for('usuarios'))
    
    cur.close()
    return render_template('usuario_form.html', usuario=usuario)

@app.route('/usuario/eliminar/<int:user_id>', methods=['POST'])
@login_required
@role_required(['admin'])
def eliminar_usuario(user_id):
    if user_id == current_user.id:
        flash('No puedes eliminar tu propio usuario', 'danger')
        return redirect(url_for('usuarios'))
    
    cur = mysql.connection.cursor()
    
    # Liberar tickets asignados a este usuario
    cur.execute("UPDATE registros_ot SET bloqueado_por = NULL WHERE bloqueado_por = %s", (user_id,))
    
    # Eliminar usuario
    cur.execute("DELETE FROM usuarios WHERE id = %s", (user_id,))
    mysql.connection.commit()
    cur.close()
    
    flash('Usuario eliminado correctamente', 'success')
    return redirect(url_for('usuarios'))

@app.route('/reportes')
@login_required
def reportes():
    cur = mysql.connection.cursor()
    
    # Tickets por estado
    cur.execute("SELECT estado, COUNT(*) as total FROM registros_ot GROUP BY estado")
    tickets_por_estado = cur.fetchall()
    
    # Tickets por estado de gestión
    cur.execute("SELECT estado_gestion, COUNT(*) as total FROM registros_ot WHERE estado_gestion IS NOT NULL GROUP BY estado_gestion")
    tickets_por_estado_gestion = cur.fetchall()
    
    # Tickets por asesor
    cur.execute("""
        SELECT u.nombre, COUNT(*) as total 
        FROM registros_ot r
        JOIN usuarios u ON r.bloqueado_por = u.id
        GROUP BY u.nombre
        ORDER BY total DESC
    """)
    tickets_por_asesor = cur.fetchall()
    
    # Promedio de tiempo de resolución en horas por asesor
    cur.execute("""
        SELECT u.nombre, 
               COUNT(*) as tickets_resueltos,
               AVG(TIMESTAMPDIFF(HOUR, r.fecha_creacion, r.fecha_finalizacion)) as promedio_horas
        FROM registros_ot r
        JOIN usuarios u ON r.bloqueado_por = u.id
        WHERE r.estado = 'cerrado' AND r.fecha_finalizacion IS NOT NULL
        GROUP BY u.nombre
        ORDER BY promedio_horas ASC
    """)
    tiempo_promedio_por_asesor = cur.fetchall()
    
    # Tickets cerrados por día en los últimos 30 días
    cur.execute("""
        SELECT DATE(fecha_finalizacion) as fecha, COUNT(*) as total
        FROM registros_ot
        WHERE estado = 'cerrado' 
          AND fecha_finalizacion IS NOT NULL
          AND fecha_finalizacion >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        GROUP BY DATE(fecha_finalizacion)
        ORDER BY fecha ASC
    """)
    tickets_por_dia = cur.fetchall()
    
    # Actividad de los asesores en los últimos 7 días
    cur.execute("""
        SELECT u.nombre, 
               COUNT(DISTINCT h.ticket_id) as tickets_trabajados,
               COUNT(h.id) as acciones_totales
        FROM historial_tickets h
        JOIN usuarios u ON h.usuario_id = u.id
        WHERE h.fecha >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        GROUP BY u.nombre
        ORDER BY acciones_totales DESC
    """)
    actividad_asesores = cur.fetchall()
    
    # Tiempo promedio por estado de gestión
    cur.execute("""
        SELECT h1.descripcion as estado_gestion,
               AVG(TIMESTAMPDIFF(MINUTE, h1.fecha, h2.fecha)) as tiempo_promedio_minutos
        FROM historial_tickets h1
        JOIN historial_tickets h2 ON h1.ticket_id = h2.ticket_id
        WHERE h1.tipo_cambio = 'cambio_estado_gestion'
          AND h2.tipo_cambio = 'cambio_estado_gestion'
          AND h1.id < h2.id
          AND NOT EXISTS (
              SELECT 1 FROM historial_tickets h3
              WHERE h3.ticket_id = h1.ticket_id
                AND h3.tipo_cambio = 'cambio_estado_gestion'
                AND h3.id > h1.id AND h3.id < h2.id
          )
        GROUP BY h1.descripcion
    """)
    tiempo_por_estado_gestion = cur.fetchall()
    
    cur.close()
    
    return render_template(
        'reportes.html',
        tickets_por_estado=tickets_por_estado,
        tickets_por_estado_gestion=tickets_por_estado_gestion,
        tickets_por_asesor=tickets_por_asesor,
        tiempo_promedio_por_asesor=tiempo_promedio_por_asesor,
        tickets_por_dia=tickets_por_dia,
        actividad_asesores=actividad_asesores,
        tiempo_por_estado_gestion=tiempo_por_estado_gestion
    )


from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, Response
import json
import time

# Actualizar la función ver_chat_completo para incluir actualización en tiempo real
@app.route('/chat_completo/<string:numero_ot>', methods=['GET'])
@login_required
def ver_chat_completo(numero_ot):
    cur = mysql.connection.cursor()
    
    # Obtener información del ticket
    cur.execute("""
        SELECT r.*, u.nombre as nombre_asesor 
        FROM registros_ot r
        LEFT JOIN usuarios u ON r.bloqueado_por = u.id
        WHERE r.ticket = %s
    """, (numero_ot,))
    ticket = cur.fetchone()
    
    if not ticket:
        flash('Ticket no encontrado', 'danger')
        return redirect(url_for('tickets'))
    
    # Determinar qué chat_id usar
    chat_id_to_use = ticket['chat_id'] if ticket['chat_id'] else str(ticket['id'])
    
    # Consulta de mensajes mejorada con diagnóstico
    cur.execute("""
        SELECT mt.*, 
               CASE 
                   WHEN mt.enviado_por LIKE 'Técnico:%%' THEN 'tecnico'
                   ELSE 'asesor'
               END AS tipo_remitente
        FROM mensajes_tecnicos mt
        WHERE mt.chat_id = %s
        ORDER BY mt.fecha_envio ASC
    """, (chat_id_to_use,))
    mensajes = cur.fetchall()
    
    # Si no encontramos mensajes y existe un chat_id diferente, intentar con ese
    if not mensajes and ticket['chat_id'] and ticket['chat_id'] != str(ticket['id']):
        app.logger.info(f"No se encontraron mensajes con chat_id={chat_id_to_use}, probando con ID del ticket={ticket['id']}")
        cur.execute("""
            SELECT mt.*, 
                   CASE 
                       WHEN mt.enviado_por LIKE 'Técnico:%%' THEN 'tecnico'
                       ELSE 'asesor'
                   END AS tipo_remitente
            FROM mensajes_tecnicos mt
            WHERE mt.chat_id = %s
            ORDER BY mt.fecha_envio ASC
        """, (str(ticket['id']),))
        mensajes = cur.fetchall()
    
    # Consulta de estadísticas mejorada con diagnóstico
    cur.execute("""
        SELECT 
            COUNT(*) as total_mensajes,
            COUNT(CASE WHEN enviado_por LIKE 'Técnico:%%' THEN 1 END) as mensajes_tecnicos,
            COUNT(CASE WHEN enviado_por NOT LIKE 'Técnico:%%' THEN 1 END) as mensajes_asesores,
            MIN(fecha_envio) as primer_mensaje,
            MAX(fecha_envio) as ultimo_mensaje,
            
            -- Diagnóstico: Obtener patrones usados en los remitentes
            GROUP_CONCAT(DISTINCT LEFT(enviado_por, 10)) as patrones_remitentes
        FROM mensajes_tecnicos
        WHERE chat_id = %s
    """, (chat_id_to_use,))
    estadisticas = cur.fetchone()
    
    # Debug: Añadir log para verificar
    app.logger.info(f"Estadísticas del chat {numero_ot}: {estadisticas}")
    app.logger.info(f"Total mensajes: {len(mensajes)}")
    
    # Verificar mensajes de técnicos específicamente
    mensajes_tecnicos = [m for m in mensajes if 'Técnico:' in m['enviado_por']]
    app.logger.info(f"Mensajes de técnicos encontrados: {len(mensajes_tecnicos)}")
    if mensajes_tecnicos:
        app.logger.info(f"Ejemplo de mensaje técnico: {mensajes_tecnicos[0]['enviado_por']}")
    
    cur.close()
    
    return render_template('chat_completo.html', 
                          ticket=ticket, 
                          mensajes=mensajes,
                          estadisticas=estadisticas)
# Función para guardar mensajes de forma consistente
def guardar_mensaje(chat_id, mensaje, enviado_por):
    """
    Guarda un mensaje en la base de datos usando el chat_id apropiado
    
    Args:
        chat_id: ID de chat a usar (puede ser el ID del ticket o el chat_id de Telegram)
        mensaje: Contenido del mensaje
        enviado_por: Nombre o identificador de quien envía el mensaje
    
    Returns:
        bool: True si se guardó correctamente, False en caso contrario
    """
    try:
        cur = mysql.connection.cursor()
        cur.execute("""
            INSERT INTO mensajes_tecnicos (chat_id, mensaje, enviado_por, fecha_envio)
            VALUES (%s, %s, %s, NOW())
        """, (chat_id, mensaje, enviado_por))
        mysql.connection.commit()
        cur.close()
        return True
    except Exception as e:
        app.logger.error(f"Error al guardar mensaje: {str(e)}")
        return False
def obtener_mensajes(ticket_id=None, chat_id=None):
    """
    Obtiene todos los mensajes asociados a un ticket
    
    Args:
        ticket_id: ID del ticket
        chat_id: ID del chat de Telegram (opcional)
    
    Returns:
        list: Lista de mensajes o lista vacía si no hay mensajes
    """
    cur = mysql.connection.cursor()
    mensajes = []
    
    # Si tenemos un ticket_id, intentamos buscar por el ID del ticket primero
    if ticket_id:
        cur.execute("""
            SELECT * FROM mensajes_tecnicos
            WHERE chat_id = %s
            ORDER BY fecha_envio ASC
        """, (str(ticket_id),))
        mensajes = cur.fetchall()
    
    # Si no encontramos mensajes y tenemos un chat_id alternativo, intentamos con ese
    if not mensajes and chat_id:
        cur.execute("""
            SELECT * FROM mensajes_tecnicos
            WHERE chat_id = %s
            ORDER BY fecha_envio ASC
        """, (chat_id,))
        mensajes = cur.fetchall()
    
    cur.close()
    return mensajes
# Añadir un endpoint para obtener mensajes nuevos (para actualización en tiempo real)
@app.route('/api/chat/nuevos_mensajes/<string:numero_ot>/<int:ultimo_id>', methods=['GET'])
@login_required
def nuevos_mensajes(numero_ot, ultimo_id):
    cur = mysql.connection.cursor()
    
    # Primero obtener el ID del ticket
    cur.execute("SELECT id, chat_id FROM registros_ot WHERE ticket = %s", (numero_ot,))
    ticket = cur.fetchone()
    
    if not ticket:
        cur.close()
        return jsonify({'error': 'Ticket no encontrado'}), 404
    
    # Determinar qué chat_id usar
    chat_id_to_use = ticket['chat_id'] if ticket['chat_id'] else str(ticket['id'])
    
    # Verificar mensajes existentes para diagnóstico
    cur.execute("""
        SELECT COUNT(*) as total_mensajes,
               COUNT(CASE WHEN enviado_por LIKE 'Técnico:%%' THEN 1 END) as mensajes_tecnicos
        FROM mensajes_tecnicos
        WHERE chat_id = %s
    """, (chat_id_to_use,))
    diagnostico = cur.fetchone()
    app.logger.info(f"Diagnóstico para {numero_ot}: Total={diagnostico['total_mensajes']}, Técnicos={diagnostico['mensajes_tecnicos']}")
    
    # Consulta para nuevos mensajes con formato de fecha corregido
    cur.execute("""
        SELECT mt.id, mt.mensaje, mt.enviado_por, 
               DATE_FORMAT(mt.fecha_envio, '%%Y-%%m-%%d %%H:%%i:%%s') as fecha_envio,
               CASE 
                   WHEN mt.enviado_por LIKE 'Técnico:%%' THEN 'tecnico'
                   ELSE 'asesor'
               END AS tipo_remitente
        FROM mensajes_tecnicos mt
        WHERE mt.chat_id = %s AND mt.id > %s
        ORDER BY mt.fecha_envio ASC
    """, (chat_id_to_use, ultimo_id))
    
    nuevos_mensajes = cur.fetchall()
    app.logger.info(f"Nuevos mensajes encontrados: {len(nuevos_mensajes)}")
    
    # Si no hay mensajes y tenemos un chat_id alternativo, intentar con ese
    if not nuevos_mensajes and ticket['chat_id'] and ticket['chat_id'] != str(ticket['id']):
        chat_id_alt = str(ticket['id'])
        app.logger.info(f"No se encontraron mensajes con chat_id={chat_id_to_use}, probando con {chat_id_alt}")
        
        cur.execute("""
            SELECT mt.id, mt.mensaje, mt.enviado_por, 
                   DATE_FORMAT(mt.fecha_envio, '%%Y-%%m-%%d %%H:%%i:%%s') as fecha_envio,
                   CASE 
                       WHEN mt.enviado_por LIKE 'Técnico:%%' THEN 'tecnico'
                       ELSE 'asesor'
                   END AS tipo_remitente
            FROM mensajes_tecnicos mt
            WHERE mt.chat_id = %s AND mt.id > %s
            ORDER BY mt.fecha_envio ASC
        """, (chat_id_alt, ultimo_id))
        
        nuevos_mensajes = cur.fetchall()
        app.logger.info(f"Con chat_id alternativo: {len(nuevos_mensajes)} mensajes encontrados")
    
    cur.close()
    
    return jsonify({'mensajes': nuevos_mensajes})

# Endpoint SSE (Server-Sent Events) para streaming de mensajes en tiempo real
@app.route('/api/chat/stream/<string:numero_ot>', methods=['GET'])
@login_required
def stream_chat(numero_ot):
    # Obtener el último ID una sola vez al inicio, dentro del contexto de la solicitud
    ultimo_id = int(request.args.get('ultimo_id', 0))
    app.logger.info(f"Iniciando stream para {numero_ot} desde ID {ultimo_id}")
    
    def event_stream():
        nonlocal ultimo_id
        
        while True:
            try:
                # Obtener nuevos mensajes
                with app.app_context():
                    cur = mysql.connection.cursor()
                    
                    # Obtener ID del ticket y chat_id
                    cur.execute("SELECT id, chat_id FROM registros_ot WHERE ticket = %s", (numero_ot,))
                    ticket = cur.fetchone()
                    
                    if ticket:
                        # Determinar qué chat_id usar
                        chat_id_to_use = ticket['chat_id'] if ticket['chat_id'] else str(ticket['id'])
                        
                        # Consulta con formato de fecha corregido
                        cur.execute("""
                            SELECT mt.id, mt.mensaje, mt.enviado_por, 
                                   DATE_FORMAT(mt.fecha_envio, '%%Y-%%m-%%d %%H:%%i:%%s') as fecha_envio,
                                   CASE 
                                       WHEN mt.enviado_por LIKE 'Técnico:%%' THEN 'tecnico'
                                       ELSE 'asesor'
                                   END AS tipo_remitente
                            FROM mensajes_tecnicos mt
                            WHERE mt.chat_id = %s AND mt.id > %s
                            ORDER BY mt.fecha_envio ASC
                        """, (chat_id_to_use, ultimo_id))
                        
                        nuevos_mensajes = cur.fetchall()
                        
                        # Si no hay mensajes y tenemos un chat_id alternativo, intentar con ese
                        if not nuevos_mensajes and ticket['chat_id'] and ticket['chat_id'] != str(ticket['id']):
                            chat_id_alt = str(ticket['id'])
                            
                            cur.execute("""
                                SELECT mt.id, mt.mensaje, mt.enviado_por, 
                                       DATE_FORMAT(mt.fecha_envio, '%%Y-%%m-%%d %%H:%%i:%%s') as fecha_envio,
                                       CASE 
                                           WHEN mt.enviado_por LIKE 'Técnico:%%' THEN 'tecnico'
                                           ELSE 'asesor'
                                       END AS tipo_remitente
                                FROM mensajes_tecnicos mt
                                WHERE mt.chat_id = %s AND mt.id > %s
                                ORDER BY mt.fecha_envio ASC
                            """, (chat_id_alt, ultimo_id))
                            
                            nuevos_mensajes = cur.fetchall()
                        
                        cur.close()
                        
                        if nuevos_mensajes:
                            ultimo_id = nuevos_mensajes[-1]['id']
                            app.logger.info(f"Stream: Enviando {len(nuevos_mensajes)} mensajes nuevos. Nuevo último ID: {ultimo_id}")
                            yield f"data: {json.dumps({'mensajes': nuevos_mensajes})}\n\n"
            except Exception as e:
                app.logger.error(f"Error en stream: {str(e)}")
                # Continuar a pesar del error
            
            time.sleep(2)  # Esperar 2 segundos antes de revisar nuevamente
    
    return Response(event_stream(), mimetype="text/event-stream")
@app.route('/chat_telegram')
@login_required
def chat_telegram():
    """
    Muestra una lista de todos los tickets con chats de Telegram activos
    """
    cur = mysql.connection.cursor()
    
    # Obtener tickets con chat_id de Telegram
    cur.execute("""
        SELECT r.*, 
               u.nombre as nombre_asesor,
               (SELECT COUNT(*) FROM mensajes_tecnicos mt WHERE mt.chat_id = r.id) as total_mensajes,
               (SELECT MAX(fecha_envio) FROM mensajes_tecnicos mt WHERE mt.chat_id = r.id) as ultimo_mensaje
        FROM registros_ot r
        LEFT JOIN usuarios u ON r.bloqueado_por = u.id
        WHERE r.chat_id IS NOT NULL AND r.chat_id != ''
        ORDER BY ultimo_mensaje DESC
    """)
    tickets = cur.fetchall()
    
    cur.close()
    
    return render_template('chat_telegram.html', tickets=tickets)


# En la función enviar_mensaje, si estás insertando registros en mensajes_tecnicos:
@app.route('/enviar_mensaje', methods=['POST'])
@login_required
def enviar_mensaje():
    # Obtener datos del formulario
    chat_id = request.form.get('chat_id')
    mensaje = request.form.get('mensaje')
    ticket_numero = request.form.get('ticket_numero')  # Asumiendo que también pasas el número de ticket
    
    if not chat_id or not mensaje:
        flash('Chat ID y mensaje son requeridos', 'danger')
        return redirect(url_for('dashboard'))
    
    # Obtener el ID numérico del ticket
    cur = mysql.connection.cursor()
    if ticket_numero:
        cur.execute("SELECT id FROM registros_ot WHERE ticket = %s", (ticket_numero,))
        ticket = cur.fetchone()
        ticket_id = ticket['id'] if ticket else None
    else:
        ticket_id = None
    
    # Llamar a la función para enviar el mensaje
    try:
        from telegram import Bot
        bot = Bot(token=TELEGRAM_TOKEN)
        bot.send_message(chat_id=chat_id, text=mensaje)
        
        # Registrar el mensaje en la base de datos
        if ticket_id:
            cur.execute("""
                INSERT INTO mensajes_tecnicos (chat_id, mensaje, enviado_por, fecha_envio)
                VALUES (%s, %s, %s, NOW())
            """, ( chat_id, mensaje, current_user.nombre))
        else:
            cur.execute("""
                INSERT INTO mensajes_tecnicos (chat_id, mensaje, enviado_por, fecha_envio)
                VALUES (%s, %s, %s, NOW())
            """, (chat_id, mensaje, current_user.nombre))
        
        mysql.connection.commit()
        cur.close()
        
        flash('Mensaje enviado correctamente', 'success')
    except Exception as e:
        flash(f'Error al enviar mensaje: {e}', 'danger')
    
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    # Crear tablas si no existen
    with app.app_context():
        cur = mysql.connection.cursor()
        
        # Tabla de usuarios si no existe
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                nombre VARCHAR(100) NOT NULL,
                role ENUM('admin', 'asesor') NOT NULL DEFAULT 'asesor',
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Tabla para registros_ot si no existe
        cur.execute("""
            CREATE TABLE IF NOT EXISTS registros_ot (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ticket VARCHAR(50) UNIQUE NOT NULL,
                nombre_cliente VARCHAR(100) NOT NULL,
                telefono VARCHAR(20),
                chat_id VARCHAR(50),
                detalles TEXT,
                estado ENUM('pendiente', 'en_proceso', 'en_espera', 'resuelto', 'cerrado') NOT NULL DEFAULT 'pendiente',
                estado_gestion VARCHAR(100) NULL,
                bloqueado_por INT NULL,
                fecha_bloqueo DATETIME NULL,
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha_actualizacion DATETIME NULL,
                fecha_finalizacion DATETIME NULL
            )
        """)
        
        # Tabla para historial de tickets
        cur.execute("""
            CREATE TABLE IF NOT EXISTS historial_tickets (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ticket_id VARCHAR(50) NOT NULL,
                usuario_id INT NOT NULL,
                nombre_usuario VARCHAR(100) NOT NULL,
                tipo_cambio ENUM('asignacion', 'liberacion', 'cambio_estado', 'cambio_estado_gestion', 'mensaje', 'liberacion_automatica') NOT NULL,
                descripcion TEXT NOT NULL,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Verificar si hay un usuario admin, si no crear uno predeterminado
        cur.execute("SELECT COUNT(*) as total FROM usuarios WHERE role = 'admin'")
        if cur.fetchone()['total'] == 0:
            hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
            cur.execute("""
                INSERT INTO usuarios (username, password, nombre, role)
                VALUES ('admin', %s, 'Administrador', 'admin')
            """, (hashed_password,))
        
        # Actualizar tabla registros_ot para agregar campos necesarios si no existen
        try:
            # Verificar si existe el campo estado_gestion
            cur.execute("SHOW COLUMNS FROM registros_ot LIKE 'estado_gestion'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE registros_ot ADD COLUMN estado_gestion VARCHAR(100) NULL AFTER estado")
            
            # Verificar otros campos
            cur.execute("SHOW COLUMNS FROM registros_ot LIKE 'bloqueado_por'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE registros_ot ADD COLUMN bloqueado_por INT NULL")
                cur.execute("ALTER TABLE registros_ot ADD COLUMN fecha_bloqueo DATETIME NULL")
                cur.execute("ALTER TABLE registros_ot ADD COLUMN fecha_actualizacion DATETIME NULL")
                cur.execute("ALTER TABLE registros_ot ADD COLUMN fecha_finalizacion DATETIME NULL")
        except Exception as e:
            app.logger.error(f"Error actualizando tabla: {e}")
            pass
        
        mysql.connection.commit()
        cur.close()
    
    # Ejecutar la aplicación en la red local
    app.run(host='0.0.0.0', port=8080, debug=True)
