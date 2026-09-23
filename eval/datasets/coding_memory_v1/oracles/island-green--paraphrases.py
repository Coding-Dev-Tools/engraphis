"""Immutable oracle for island-green:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-island-green', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
