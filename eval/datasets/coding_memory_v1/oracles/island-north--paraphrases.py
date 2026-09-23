"""Immutable oracle for island-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-island-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
