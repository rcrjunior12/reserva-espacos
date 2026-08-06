# Reserva de Espaços — ONG

Sistema web simples para o gestor da ONG controlar as reservas da quadra de cimento, quadra de areia, salas e área de churrasco. Funciona em qualquer navegador (celular, tablet ou computador) — não é um app nativo, é um site responsivo.

## O que o sistema faz

- Login único do gestor (dá para trocar a senha e, se precisar depois, criar mais usuários direto no banco).
- Cadastro de **espaços** (quadra de cimento, quadra de areia, salas, churrasqueira), com as atividades permitidas em cada um.
- Cadastro de **ministérios** da igreja, para vincular a cada reserva.
- Cadastro de **reservas**: espaço, atividade, data/horário, responsável, contato, ministério, status (confirmada/pendente/cancelada).
- Controle de **pagamento**: gratuito ou pago, forma de pagamento, valor, se já foi recebido, e upload do **comprovante** (foto ou PDF).
- **Detecção automática de conflito de horário** — não deixa marcar duas reservas no mesmo espaço com horários que se sobrepõem.
- **Calendário visual** (mensal, semanal ou lista) e listagem com filtros por espaço, ministério, status e período.
- Painel inicial com próximas reservas e pagamentos pendentes.
- Layout responsivo, com menu inferior de atalhos no celular.

## Como rodar localmente (agora)

Pré-requisitos: Python 3.10+ instalado.

```bash
cd reserva-espacos
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Acesse **http://localhost:5000** no navegador do computador.

**Para acessar do celular na mesma rede Wi-Fi:** descubra o IP do computador (ex: `192.168.0.10`) e acesse `http://192.168.0.10:5000` pelo navegador do celular.

### Login inicial

- **E-mail:** `gestor@ong.org`
- **Senha:** `mudar123`

Troque a senha assim que entrar (menu do usuário, no canto superior direito → "Alterar senha").

### Primeiros passos

1. Vá em **Espaços** e confira/ajuste a Sala 1 (renomeie ou adicione outras salas conforme a ONG tiver — não tem limite de quantidade).
2. Vá em **Ministérios** e ajuste a lista para os ministérios reais da igreja.
3. Comece a lançar as reservas em **Reservas → Nova reserva** ou direto pelo **Calendário** (clique numa data).

## Estrutura do projeto

```
reserva-espacos/
├── app.py              # rotas e lógica principal
├── models.py            # tabelas do banco (Usuário, Espaço, Ministério, Reserva)
├── requirements.txt
├── templates/            # páginas HTML
├── static/css/style.css
├── uploads/              # comprovantes de pagamento enviados (criado automaticamente)
├── reservas.db            # banco de dados SQLite (criado automaticamente no 1º acesso)
├── Procfile               # para deploy (Render/Railway)
└── render.yaml             # exemplo de configuração para deploy no Render
```

O banco é um arquivo SQLite único (`reservas.db`) — simples, sem precisar instalar servidor de banco de dados separado.

## Quando for para produção (hospedar de verdade)

Para o gestor acessar de qualquer lugar pelo celular, o sistema precisa estar publicado num servidor com endereço fixo na internet. Duas opções simples e com plano gratuito/barato:

### Opção A — Render.com
1. Crie uma conta em render.com e um repositório Git (GitHub) com esta pasta.
2. No Render, "New Web Service" → conecte o repositório.
3. Use o `render.yaml` incluso como referência (ele já configura disco persistente, necessário para o banco e os comprovantes não se perderem a cada deploy). **Atenção:** disco persistente exige um plano pago (a partir de poucos dólares/mês) — o plano gratuito apaga os arquivos a cada reinício.

### Opção B — Railway.app
1. Crie uma conta em railway.app, conecte o repositório.
2. Adicione um **Volume** apontando para `/data` e configure as variáveis de ambiente:
   - `DATABASE_URL=sqlite:////data/reservas.db`
   - `UPLOAD_FOLDER=/data/uploads`
3. O Railway detecta o `Procfile` automaticamente.

### Recomendação para o futuro
Com o uso crescendo (mais reservas, mais fotos de comprovante), vale migrar de SQLite para PostgreSQL (o Render e o Railway oferecem isso com poucos cliques) — o código já usa SQLAlchemy, então a troca é só mudar a variável `DATABASE_URL`, sem reescrever telas.

## Segurança

- Troque a senha padrão assim que rodar pela primeira vez.
- Troque também o valor de `SECRET_KEY` antes de publicar na internet (defina como variável de ambiente `SECRET_KEY`).
- Os comprovantes de pagamento só podem ser vistos por quem estiver logado no sistema.
