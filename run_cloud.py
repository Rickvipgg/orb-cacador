from cloud_sync import pull_state, push_state
from shopee_cacador_v3 import main

if __name__ == "__main__":
    pull_state()
    try:
        main(open_panel=False)
    finally:
        # Se o processamento gerou/alterou estado, tenta persistir mesmo que um passo posterior falhe.
        push_state()
