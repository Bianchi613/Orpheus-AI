# Segurança do Orpheus-AI

**Atualizado em 2026-11-03.** Documenta o [PR #1](https://github.com/Bianchi613/Orpheus-AI/pull/1), mesclado à `main`. Correção revisada estaticamente; os testes incluídos não foram executados neste atendimento.

## Correção: acesso web e SSRF

A ferramenta `fetch_url` em `api.py` agora chama `_validate_public_url` antes de fazer uma requisição HTTP. A validação:

- permite apenas URLs HTTP e HTTPS válidas;
- recusa credenciais embutidas na URL;
- resolve o host e rejeita endereços não globais, como loopback, redes privadas e faixas reservadas;
- valida novamente URLs recebidas em redirecionamentos HTTP, via `_ValidatedRedirectHandler`;
- aceita apenas métodos GET e HEAD no `fetch_url`.

Isso **reduz o risco de SSRF**, em que um agente induzido por conteúdo não confiável poderia acessar serviços internos. Não é uma garantia completa contra DNS rebinding, alterações de resolução entre validação e conexão ou outras técnicas de contorno de rede.

## Configuração

Por padrão, o agente **não** deve alcançar `http://localhost` ou IPs privados usando essa ferramenta. Existe uma exceção explícita para cenários locais confiáveis:

```dotenv
ORPHEUS_ALLOW_PRIVATE_WEB_URLS=1
```

**Não ative essa exceção em agentes que recebam links, páginas ou comandos de terceiros.** Para produção, recomenda-se política de saída de rede (egress filtering), DNS controlado, execução com privilégio mínimo e isolamento do processo. Essa variável não é necessária para usar modelos Ollama ou OpenAI pela configuração normal do próprio agente; controla apenas a validação dessa ferramenta web.

## Verificação

No diretório raiz do repositório:

```bash
python3 -m py_compile ai.py config.py api.py executor.py state.py
python3 -m unittest test_web_security.py
```

Os testes incluídos simulam resolução DNS e verificam rejeição de localhost, IP privado, URL com credenciais e redirecionamento para rede local, bem como aceitação de um endereço público de exemplo. Eles não fazem uma prova de segurança em rede real.

Testes integrados sugeridos:

1. Confirme que `/web` recusa `http://127.0.0.1:8000` e `http://localhost`.
2. Confirme que uma página HTTPS pública pode ser consultada.
3. Confirme que um redirecionamento para um host privado é bloqueado.
4. Use um ambiente isolado para exercitar tentativas de DNS rebinding e redirecionamentos encadeados.
5. Revise logs para que não contenham tokens, credenciais ou saídas sensíveis.

## Execução de comandos locais: risco separado

O `executor.py` já tinha bloqueios e aprovação humana para determinadas operações. **Não modifiquei o executor neste PR.** Filtrar comandos por padrões ou confirmar ações não é substituto de sandbox, contas de sistema restritas, isolamento, teste adversarial ou backups. Um agente com acesso ao shell continua sendo um componente de alto risco.

## Histórico e medidas adicionais

[PR #1: proteção contra acesso a redes internas](https://github.com/Bianchi613/Orpheus-AI/pull/1). Planeje pinagem de IP no momento da conexão ou firewall de saída como defesa em profundidade, além de testes automatizados de segurança do executor. Se precisar acessar serviços internos de propósito, faça isso apenas num perfil local isolado e com permissão explícita.