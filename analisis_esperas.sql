-- analisis_esperas.sql  (SOLO LECTURA: solo SELECT, base webchat)
-- Uso:  psql "<conexion webchat>" -f analisis_esperas.sql
-- Ajusta el rango en 'rango' si quieres otro periodo (por defecto, ultimos 7 dias, hora de Lima).

\echo '=== 1. Espera real del cliente hasta la primera respuesta de un asesor (minutos), por dia ==='
WITH base AS (
  SELECT c.id, c.campana_id, c.asesor_id,
         COALESCE(c.entrada_cola, c.iniciado_en) AS desde,
         c.asignado_en,
         (SELECT min(m.enviado_en) FROM mensajes m
           WHERE m.conversacion_id = c.id AND m.emisor_tipo IN ('asesor','supervisor')
             AND m.enviado_en >= c.asignado_en) AS primera
  FROM conversaciones c
  WHERE c.asignado_en >= now() - interval '7 days'
    AND c.asignado_en IS NOT NULL
), t AS (
  SELECT *,
         EXTRACT(EPOCH FROM (asignado_en - desde))/60 AS min_cola,
         EXTRACT(EPOCH FROM (primera - asignado_en))/60 AS min_bandeja,
         EXTRACT(EPOCH FROM (primera - desde))/60 AS min_total
  FROM base
)
SELECT (desde AT TIME ZONE 'America/Lima')::date AS dia,
       count(*) AS conversaciones,
       count(primera) AS con_respuesta,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY min_cola)::numeric,1) AS cola_p50,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY min_cola)::numeric,1) AS cola_p90,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY min_bandeja)::numeric,1) AS bandeja_p50,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY min_bandeja)::numeric,1) AS bandeja_p90,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY min_total)::numeric,1) AS total_p90,
       count(*) FILTER (WHERE min_total > 15) AS mas_de_15min
FROM t GROUP BY 1 ORDER BY 1 DESC;

\echo '=== 2. Espera total (p50/p90) por hora de Lima, ultimos 7 dias ==='
WITH base AS (
  SELECT COALESCE(c.entrada_cola, c.iniciado_en) AS desde,
         (SELECT min(m.enviado_en) FROM mensajes m
           WHERE m.conversacion_id = c.id AND m.emisor_tipo IN ('asesor','supervisor')
             AND m.enviado_en >= c.asignado_en) AS primera
  FROM conversaciones c
  WHERE c.asignado_en >= now() - interval '7 days'
)
SELECT EXTRACT(HOUR FROM desde AT TIME ZONE 'America/Lima')::int AS hora,
       count(*) AS n,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (primera-desde))/60)::numeric,1) AS p50_min,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (primera-desde))/60)::numeric,1) AS p90_min
FROM base WHERE primera IS NOT NULL
GROUP BY 1 ORDER BY 1;

\echo '=== 3. Por asesor: primera respuesta en bandeja (min), ultimos 7 dias ==='
WITH base AS (
  SELECT c.asesor_id, c.asignado_en,
         (SELECT min(m.enviado_en) FROM mensajes m
           WHERE m.conversacion_id = c.id AND m.emisor_tipo IN ('asesor','supervisor')
             AND m.enviado_en >= c.asignado_en) AS primera
  FROM conversaciones c
  WHERE c.asignado_en >= now() - interval '7 days' AND c.asesor_id IS NOT NULL
)
SELECT a.nombre_display, count(*) AS asignadas, count(primera) AS respondidas,
       count(*) - count(primera) AS sin_respuesta,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (primera-asignado_en))/60)::numeric,1) AS p50_min,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (primera-asignado_en))/60)::numeric,1) AS p90_min
FROM base b JOIN asesores a ON a.id = b.asesor_id
GROUP BY a.nombre_display ORDER BY p90_min DESC NULLS LAST;

\echo '=== 4. Conversaciones "asignada" ahora sin respuesta (las que inflan la espera estimada) ==='
SELECT c.id, a.nombre_display AS asesor, c.asignado_en AT TIME ZONE 'America/Lima' AS asignado_lima,
       round(EXTRACT(EPOCH FROM (now() - c.asignado_en))/60) AS min_esperando,
       c.pausada_en IS NOT NULL AS pausada
FROM conversaciones c JOIN asesores a ON a.id = c.asesor_id
WHERE c.estado = 'asignada'
  AND NOT EXISTS (SELECT 1 FROM mensajes m WHERE m.conversacion_id = c.id
                  AND m.emisor_tipo IN ('asesor','supervisor') AND m.enviado_en >= c.asignado_en)
ORDER BY c.asignado_en;

\echo '=== 5. Asignadas hace mas de 60 min sin ninguna respuesta del asesor (posibles fantasmas), ultimos 7 dias ==='
SELECT (c.asignado_en AT TIME ZONE 'America/Lima')::date AS dia, a.nombre_display AS asesor, count(*) AS n
FROM conversaciones c JOIN asesores a ON a.id = c.asesor_id
WHERE c.asignado_en >= now() - interval '7 days'
  AND c.asignado_en < now() - interval '60 minutes'
  AND NOT EXISTS (SELECT 1 FROM mensajes m WHERE m.conversacion_id = c.id
                  AND m.emisor_tipo IN ('asesor','supervisor') AND m.enviado_en >= c.asignado_en)
GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC;
