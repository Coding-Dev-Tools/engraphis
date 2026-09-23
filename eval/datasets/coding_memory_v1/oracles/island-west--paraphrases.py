"""Immutable oracle for island-west:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-island-west', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
