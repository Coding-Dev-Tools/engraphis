"""Immutable oracle for atlas-west:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-atlas-west', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
