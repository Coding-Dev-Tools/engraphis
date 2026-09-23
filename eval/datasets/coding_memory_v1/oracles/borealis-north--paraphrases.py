"""Immutable oracle for borealis-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-borealis-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
