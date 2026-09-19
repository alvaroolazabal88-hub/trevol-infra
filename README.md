# TREVol / Macro Cocina Fit — infra en AWS

Sitio estático (S3 + CloudFront) + backend de pedidos serverless
(API Gateway + Lambda + DynamoDB). Sin EC2, sin ALB, sin NAT Gateway.
Costo estimado corriendo todo el mes: **$0.50 – $1.50**, con alarma en $3.

## Instalar Terraform (una vez, en tu máquina — no en este chat)

```bash
brew install terraform          # Mac
# o: https://developer.hashicorp.com/terraform/install
terraform version                # confirmar que corrió
```

También necesitas la AWS CLI configurada con tus credenciales:
```bash
aws configure
```

## Paso 1 — Bootstrap (SOLO la primera vez)

Crea el bucket de estado y la tabla de lock. Esto usa estado local a propósito.

```bash
cd bootstrap
terraform init
terraform apply
```

Confirma que los nombres de los outputs (`trevol-terraform-state`,
`trevol-terraform-lock`) coinciden con lo que está escrito en
`envs/prod/providers.tf`. Si cambiaste `project` del default `trevol`,
ajusta ese archivo a mano.

## Paso 2 — Configurar variables

```bash
cd ../envs/prod
cp terraform.tfvars.example terraform.tfvars
```

Edita `terraform.tfvars`: pon tu correo real en `alert_email` (ahí llegan
los avisos de gasto). Deja `domain_active = false` por ahora.

## Paso 3 — Primer apply (sin dominio todavía)

```bash
terraform init
terraform apply
```

Al terminar, el output `site_url` te da una URL tipo
`https://d111111abcdef8.cloudfront.net` — el sitio ya está arriba y
funcionando ahí, con el formulario de pedidos incluido.

## Paso 4 — Cuando llegue el correo de AWS confirmando el dominio

Edita `terraform.tfvars`:
```hcl
domain_active = true
```

```bash
terraform apply
```

Esto conecta Route 53, valida el certificado HTTPS automáticamente
(puede tardar unos minutos en el primer apply mientras ACM valida) y
mueve el sitio a `https://trevolcamaguey.com`.

## Actualizar el contenido de la página

Solo edita los archivos dentro de `/site` y vuelve a correr:
```bash
terraform apply
```
Terraform sube solo lo que cambió (compara por hash).

## Bajar todo (para no gastar nada mientras no se usa)

```bash
cd envs/prod
terraform destroy
```
El bootstrap (bucket de estado) se queda — nunca lo destruyas salvo que
quieras abandonar el proyecto por completo.

## Estructura

```
bootstrap/        Bucket S3 + tabla DynamoDB para el estado de Terraform (una vez)
envs/prod/        El "entrypoint" real: une todos los módulos
modules/frontend/ S3 + CloudFront (el sitio)
modules/dns/      Route 53 + certificado ACM (HTTPS)
modules/backend-api/ DynamoDB + Lambda + API Gateway (el formulario de pedidos)
modules/budget/   Alarma de AWS Budgets a $3/mes
lambda/order_handler/ Código Python del Lambda que guarda los pedidos
site/             HTML/CSS/JS del sitio — esto es lo que se sube a S3
```
