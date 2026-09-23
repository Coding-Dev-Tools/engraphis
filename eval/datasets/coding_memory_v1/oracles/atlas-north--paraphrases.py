"""Immutable oracle for atlas-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-atlas-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
