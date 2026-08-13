# ORB Caçador — versão nuvem

Esta pasta já está preparada para o fluxo:

Shopee -> GitHub Actions -> Supabase Storage -> painel ORB com login.

## O que foi corrigido
- Corrigido o problema de encoding no Windows que fazia o log com emoji gerar um falso "Erro no download" mesmo quando o feed havia sido baixado.
- Criado `saida/latest.json` para o painel online.
- URLs dos feeds e affiliate ID podem vir de GitHub Secrets; não precisam ficar no código.
- `historico.sqlite` e `feed_status.json` podem ser persistidos no Supabase Storage.

## Buckets no Supabase
Crie dois buckets PRIVADOS:
- `orb-state` — guarda `historico.sqlite` e `feed_status.json`; somente o backend usa.
- `orb-panel` — guarda `latest.json`; o usuário logado no painel pode ler.

Depois crie uma policy de SELECT em `storage.objects` para usuários `authenticated` apenas no bucket `orb-panel`:

```sql
create policy "ORB painel autenticado"
on storage.objects
for select
to authenticated
using (bucket_id = 'orb-panel');
```

Crie apenas a sua conta em Authentication e depois desative novos cadastros públicos.

## GitHub Secrets necessários
No repositório: Settings -> Secrets and variables -> Actions -> New repository secret.

- `SHOPEE_AFFILIATE_ID`
- `SHOPEE_SUB_ID`
- `SHOPEE_FEED_1_URL`
- `SHOPEE_FEED_2_URL`
- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY`

Nunca coloque `SUPABASE_SECRET_KEY` no site/pasta `web`.

## Painel
Copie `web/config.example.js` para `web/config.js` e preencha SOMENTE:
- URL do Supabase
- Publishable key do Supabase

A publishable key é própria para frontend. A secret key fica só no GitHub Actions.

## Agendamento
O workflow está configurado como `5 * * * *`: a cada hora no minuto :05.

Atenção: em repositório privado do GitHub Free existe cota mensal de minutos de Actions. Se a execução média ficar alta, mude para `5 */2 * * *` (a cada 2 horas) para economizar minutos.
